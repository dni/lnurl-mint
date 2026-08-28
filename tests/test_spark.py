"""The spark backend's integration with the rest of the mint, tested at
the seams that don't need the (optional, ~20MB) breez-sdk-spark package:
the router's handling of a create_invoice that returns no preimage (the
spark backend's defining deviation - its SSP holds the preimage, see
spark.py's module docstring), the config/dispatch plumbing, and the
spark note-signature scheme. The SDK-facing functions themselves are in
test_spark_backend.py, which imports the real package and skips without
it."""

import asyncio
from hashlib import sha256
from os import urandom

import pytest
from fastapi.testclient import TestClient

import lnurl_mint.router as router_module
import lnurl_mint.spark as spark_module
from lnurl_mint.config import settings
from lnurl_mint.node import LightningBackendConfig
from tests.conftest import FakeNode, fake_invoice

SPARK_CONFIG = LightningBackendConfig(
    backend="spark", spark_mnemonic="abandon abandon abandon", spark_storage_dir="/nonexistent"
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def spark_node(node: FakeNode, monkeypatch: pytest.MonkeyPatch) -> FakeNode:
    """The standard FakeNode, with create_invoice swapped for a spark-
    shaped one: the invoice's preimage is generated and handed to the
    payer's payment (and remembered for invoice_preimage) but NOT
    returned to the router - None instead, exactly what
    spark._create_invoice_spark produces, forcing the router down its
    decode-the-hash path."""

    async def spark_create_invoice(amount_msat: int, config, memo: str = "lnurlcash mint"):
        preimage = urandom(32)
        payment_hash = sha256(preimage).hexdigest()
        node.last_preimage = preimage
        node.preimages[payment_hash] = preimage
        return fake_invoice(amount_msat, payment_hash), None

    monkeypatch.setattr(router_module, "create_invoice", spark_create_invoice)
    return node


def note_value(client: TestClient, k1: str) -> int | None:
    data = client.get(f"/w?k1={k1}").json()
    if data.get("status") == "ERROR":
        return None
    return data["maxWithdrawable"]


def test_mint_whose_backend_returns_no_preimage_keys_the_note_by_the_invoice_hash(
    client: TestClient, spark_node: FakeNode
):
    # the spark flow: /p/cb still answers with a payable invoice, but the
    # note materializes under sha256(preimage) == the invoice's payment
    # hash, which the router had to read off the invoice itself since the
    # backend returned None
    response = client.get("/p/cb?amount=5000")
    assert response.json().get("pr"), response.text
    payment_hash = sha256(spark_node.last_preimage).hexdigest()

    assert note_value(client, spark_node.last_preimage.hex()) is None
    spark_node.settled.add(payment_hash)
    assert note_value(client, spark_node.last_preimage.hex()) == 5000


def test_no_preimage_mint_serves_its_secret_only_through_verify(
    client: TestClient, spark_node: FakeNode, monkeypatch: pytest.MonkeyPatch
):
    # with comment protection (LUD-25), the preimage - which this mint
    # never held, the spark SSP generated it - is fetched live from the
    # backend (invoice_preimage) and served by LUD-21 verify once
    # settled, while the note itself is keyed by the WALLET's comment
    # hash
    monkeypatch.setattr(settings, "verify_enabled", True)
    comment_hash = urandom(32).hex()
    response = client.get(f"/p/cb?amount=5000&comment={comment_hash}")
    payment_hash = sha256(spark_node.last_preimage).hexdigest()

    verify_url = response.json()["verify"]
    unsettled = client.get(verify_url.removeprefix("http://testserver")).json()
    assert unsettled == {"status": "OK", "settled": False, "pr": response.json()["pr"]}

    spark_node.settled.add(payment_hash)
    settled = client.get(verify_url.removeprefix("http://testserver")).json()
    assert settled["settled"] is True
    assert settled["preimage"] == spark_node.last_preimage.hex()


def test_undecodable_invoice_from_a_no_preimage_backend_is_a_logged_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    async def broken_create_invoice(amount_msat: int, config, memo: str = "lnurlcash mint"):
        return "not-an-invoice", None

    monkeypatch.setattr(router_module, "create_invoice", broken_create_invoice)
    response = client.get("/p/cb?amount=5000")
    # LNURL errors answer 200 with {"status": "ERROR"} (see
    # error_handler.py) - the logged reference id is all that reaches
    # the caller
    assert response.json()["status"] == "ERROR"


def test_sat_alignment_is_rejected_before_the_sdk_is_touched():
    # the spark backend's bolt11 surface is sat-denominated, so a
    # fractional-sat amount is rejected up front rather than rounded -
    # and importantly this raises before _sdk() ever builds/connects
    # anything, so it needs no fake and no package
    with pytest.raises(ValueError, match="sat-aligned"):
        _run(spark_module._create_invoice_spark(10_500, SPARK_CONFIG, "memo"))


def test_dispatch_rejects_unknown_operations_for_spark():
    import lnurl_mint.node as node_module

    with pytest.raises(ValueError, match="not supported for backend 'spark'"):
        _run(node_module._dispatch("some_future_operation", SPARK_CONFIG, None, None))  # type: ignore[arg-type]


def test_spark_storage_dir_defaults_next_to_the_database(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "database_path", "/var/lib/mint/mint.db")
    config = settings.funding_source()
    assert config.spark_storage_dir == "/var/lib/mint/spark-wallet"

    monkeypatch.setattr(settings, "database_path", "mint.db")
    config = settings.funding_source()
    # a bare relative database_path resolves relative to the cwd, not ""
    assert config.spark_storage_dir.endswith("/spark-wallet")
    assert not config.spark_storage_dir.startswith("/var")


def test_offline_verification_is_omitted_for_spark(monkeypatch: pytest.MonkeyPatch):
    # LUD-25 fixes the note-signature digest as the "Lightning Signed
    # Message" double-sha256 construction; the spark SDK signs a
    # single-sha256 digest with no raw-digest API, so this backend must
    # not emit signatures or advertise a mintPubkey at all - both fields
    # are optional per spec, and advertising either would point wallets
    # at a verification that always fails
    from lnurl_mint.signing import mint_pubkey

    with pytest.raises(ValueError, match="offline verification"):
        _run(spark_module._sign_message_spark("LNURLcash:5000:" + "ab" * 32, SPARK_CONFIG))
    assert _run(mint_pubkey(SPARK_CONFIG)) is None


def test_mint_address_discovery_omits_mint_pubkey_for_spark(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # the /.well-known/lnurlw discovery response derives mintPubkey from
    # node info directly (not via signing.mint_pubkey, which would
    # re-fetch it) and must apply the same spark policy: no key
    # advertised when no spec-verifiable signature can ever follow -
    # even though the node identity itself is right there in nodeUri
    import lnurl_mint.router as router_module
    from lnurl_mint.node import NodeInfo

    monkeypatch.setattr(settings, "fundingsource_backend", "spark")

    async def fake_cached_fetch_node_info(config):
        return NodeInfo(alias="spark wallet", uri="02" + "ab" * 32)

    monkeypatch.setattr(router_module, "cached_fetch_node_info", fake_cached_fetch_node_info)
    data = client.get(f"/.well-known/lnurlw/{settings.username}").json()
    assert data["nodeUri"] == "02" + "ab" * 32
    assert "mintPubkey" not in data
