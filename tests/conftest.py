import os
import tempfile

# must be set before the package (and its module-level Settings/NoteStore)
# is imported, so each test session gets its own throwaway database
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import time
from hashlib import sha256
from os import urandom

import bolt11
import pytest
from bolt11.models.tags import TagChar, Tags
from bolt11.types import Bolt11
from fastapi.testclient import TestClient

import lnurl_mint.frontend as frontend_module
import lnurl_mint.router as router_module
from lnurl_mint.config import settings
from lnurl_mint.node import NodeInfo
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
        self.paid: list[str] = []
        self.fail_payments = False

    async def create_invoice(self, amount_msat: int, config, memo: str = "") -> tuple[str, bytes]:
        preimage = urandom(32)
        self.last_preimage = preimage
        return fake_invoice(amount_msat, sha256(preimage).hexdigest()), preimage

    async def is_invoice_settled(self, payment_hash: str, config) -> bool:
        return payment_hash in self.settled

    async def pay_invoice(self, invoice: str, config) -> bytes:
        if self.fail_payments:
            raise ValueError("Payment failed: no route.")
        self.paid.append(invoice)
        return urandom(32)

    async def fetch_node_info(self, config) -> NodeInfo:
        return NodeInfo(alias="fakenode", uri="02abcdef@127.0.0.1:9735", num_channels=3, num_peers=5)


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    fake = FakeNode()
    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(router_module, "create_invoice", fake.create_invoice)
    monkeypatch.setattr(router_module, "is_invoice_settled", fake.is_invoice_settled)
    monkeypatch.setattr(router_module, "pay_invoice", fake.pay_invoice)
    monkeypatch.setattr(frontend_module, "fetch_node_info", fake.fetch_node_info)
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
        response = client.get(f"/pay/cb?amount={amount_msat}")
        assert response.json().get("pr"), response.text
        preimage = node.last_preimage
        node.settled.add(sha256(preimage).hexdigest())
        return preimage.hex()

    return _mint
