import os
import tempfile

# both must run before the package (and its module-level Settings/NoteStore)
# is imported: each test session gets its own throwaway database, and
# Settings is pointed at a dotenv file that doesn't exist rather than the
# repo's own .env (see config.py) - keeps tests isolated from a developer's
# real local config (e.g. FUNDINGSOURCE_* credentials for testing against
# lnurl_server's regtest nodes, or a BASE_URL override), regardless of what
# that file contains.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LNURL_MINT_ENV_FILE"] = os.path.join(tempfile.mkdtemp(), "unused.env")
# base_url is a required setting (see config.py) - matches TestClient's own
# default host, so the many assertions elsewhere that expect "testserver"
# keep working unchanged
os.environ["BASE_URL"] = "http://testserver"

import asyncio
import time
from hashlib import sha256
from os import urandom

import bolt11
import pytest
from bolt11.models.tags import TagChar, Tags
from bolt11.types import Bolt11
from coincurve import PrivateKey
from fastapi.testclient import TestClient

import lnurl_mint.frontend as frontend_module
import lnurl_mint.router as router_module
import lnurl_mint.signing as signing_module
from lnurl_mint.config import settings
from lnurl_mint.node import NodeInfo, PaymentFailed
from lnurl_mint.server import app


def fake_invoice(amount_msat: int, payment_hash: str | None = None) -> str:
    """A syntactically-valid (but unpayable) BOLT11 invoice, for faking the
    node without needing a real one."""
    tags = Tags()
    tags.add(TagChar.payment_hash, payment_hash or urandom(32).hex())
    tags.add(TagChar.payment_secret, urandom(32).hex())
    tags.add(TagChar.description, "test")
    return bolt11.encode(
        Bolt11(currency="bc", amount_msat=amount_msat, date=int(time.time()), tags=tags),
        private_key=urandom(32).hex(),
    )


class FakeNode:
    def __init__(self) -> None:
        self.settled: set[str] = set()
        self.last_preimage: bytes = b""
        self.preimages: dict[str, bytes] = {}  # payment_hash -> preimage, for invoice_preimage
        self.paid: list[str] = []
        self.fail_payments = False
        # for simulating a *definitive* pay_invoice failure - the funding
        # source cleanly reported the payment did not go through (e.g. no
        # route), so melt should restore immediately without the fallback
        # is_payment_complete check
        self.fail_reason: str | None = None
        # for simulating an *ambiguous* pay_invoice failure - the funding
        # source secretly completed the payment despite pay_invoice raising
        # (e.g. the response was lost) - vs. the confirmation check itself
        # being unable to tell either way
        self.payment_actually_completed = False
        self.is_payment_complete_raises = False
        self.is_payment_complete_called = False
        # seconds to block inside pay_invoice before resolving - simulates
        # an in-flight payment for tests of the melt "pending" lock, which
        # otherwise has no observable window in a single-threaded test
        self.pay_delay = 0.0
        # a real keypair, standing in for the node's own identity key - lets
        # tests of LUD-XX Offline verification (signing.mint_pubkey/
        # sign_note) exercise the real "Lightning Signed Message" signing
        # and recovery logic without a real lnd/cln node
        self.identity_key = PrivateKey()

    @property
    def pubkey(self) -> str:
        return self.identity_key.public_key.format(compressed=True).hex()

    async def create_invoice(self, amount_msat: int, config, memo: str = "") -> tuple[str, bytes]:
        preimage = urandom(32)
        self.last_preimage = preimage
        payment_hash = sha256(preimage).hexdigest()
        self.preimages[payment_hash] = preimage
        return fake_invoice(amount_msat, payment_hash), preimage

    async def is_invoice_settled(self, payment_hash: str, config) -> bool:
        return payment_hash in self.settled

    async def invoice_preimage(self, payment_hash: str, config) -> bytes | None:
        if payment_hash not in self.settled:
            return None
        return self.preimages.get(payment_hash)

    async def pay_invoice(self, invoice: str, config) -> bytes:
        if self.pay_delay:
            await asyncio.sleep(self.pay_delay)
        if self.fail_reason is not None:
            raise PaymentFailed(self.fail_reason)
        if self.fail_payments:
            raise ValueError("Payment failed: no route.")
        self.paid.append(invoice)
        return urandom(32)

    async def is_payment_complete(self, payment_hash: str, config) -> bool:
        self.is_payment_complete_called = True
        if self.is_payment_complete_raises:
            raise ConnectionError("funding source unreachable")
        return self.payment_actually_completed

    async def fetch_node_info(self, config) -> NodeInfo:
        return NodeInfo(alias="fakenode", uri=f"{self.pubkey}@127.0.0.1:9735", num_channels=3, num_peers=5)

    async def sign_message(self, message: str, config) -> tuple[bytes, int]:
        # mirrors lnd's/cln's real signmessage: sign(sha256(sha256(b"Lightning
        # Signed Message:" + message))) - see node.sign_message
        digest = sha256(sha256(b"Lightning Signed Message:" + message.encode()).digest()).digest()
        raw = self.identity_key.sign_recoverable(digest, hasher=None)
        return raw[:64], raw[64]


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(router_module, "create_invoice", fake.create_invoice)
    monkeypatch.setattr(router_module, "is_invoice_settled", fake.is_invoice_settled)
    monkeypatch.setattr(router_module, "invoice_preimage", fake.invoice_preimage)
    monkeypatch.setattr(router_module, "pay_invoice", fake.pay_invoice)
    monkeypatch.setattr(router_module, "is_payment_complete", fake.is_payment_complete)
    monkeypatch.setattr(frontend_module, "fetch_node_info", fake.fetch_node_info)
    monkeypatch.setattr(signing_module, "fetch_node_info", fake.fetch_node_info)
    monkeypatch.setattr(signing_module, "sign_message", fake.sign_message)
    return fake


@pytest.fixture
def client(node: FakeNode) -> TestClient:
    return TestClient(app)


@pytest.fixture
def mint_note(client: TestClient, node: FakeNode):
    """Mint a settled bearer note of the given value and return its k1
    (the payment preimage), the way a wallet would obtain one: fetch an
    invoice from the pay callback, then 'pay' it."""

    def _mint(amount_msat: int) -> str:
        response = client.get(f"/p/cb?amount={amount_msat}")
        assert response.json().get("pr"), response.text
        preimage = node.last_preimage
        node.settled.add(sha256(preimage).hexdigest())
        return preimage.hex()

    return _mint
