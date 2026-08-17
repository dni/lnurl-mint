from bech32 import bech32_decode, convertbits
from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from lnurl_mint.frontend import lnurl_encode


def lnurl_decode(lnurl: str) -> str:
    hrp, data = bech32_decode(lnurl.lower())
    assert hrp == "lnurl" and data is not None
    decoded = convertbits(data, 5, 8, False)
    assert decoded is not None
    return bytes(decoded).decode()


def test_index_shows_title_description_qr_and_address(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert settings.title in response.text
    assert settings.description in response.text
    assert "<svg" in response.text
    assert f"{settings.username}@testserver" in response.text
    # the QR/copy string is the bech32 LNURL of the mint's LUD-16 payRequest
    assert lnurl_encode(f"http://testserver/.well-known/lnurlp/{settings.username}") in response.text


def test_index_links_to_the_wallet_with_this_mints_address(client: TestClient):
    response = client.get("/")
    assert "https://dni.github.io/lnurl-wallet" in response.text
    assert f"{settings.username}@testserver" in response.text


def test_index_shows_footer_links(client: TestClient):
    from lnurl_mint import __version__

    response = client.get("/")
    assert 'href="https://github.com/dni/lnurl-mint"' in response.text
    assert 'href="/docs"' in response.text
    assert f"v{__version__}" in response.text


def test_index_shows_node_info(client: TestClient, node):
    response = client.get("/")
    assert "fakenode" in response.text
    assert f"{node.pubkey}@127.0.0.1:9735" in response.text


def test_index_without_funding_source(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "fundingsource_backend", None)
    response = client.get("/")
    assert response.status_code == 200
    assert "No funding source configured." in response.text


def test_base_url_setting_overrides_request_url(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://mint.example")
    response = client.get("/")
    assert lnurl_encode(f"https://mint.example/.well-known/lnurlp/{settings.username}") in response.text
    assert f"{settings.username}@mint.example" in response.text

    pay = client.get(f"/.well-known/lnurlp/{settings.username}").json()
    assert pay["callback"] == "https://mint.example/p/cb"
    assert pay["withdrawLink"] == "https://mint.example/w"
    assert f"{settings.username}@mint.example" in pay["metadata"]


def test_lnurl_encode_roundtrip():
    url = "https://mint.example/p"
    lnurl = lnurl_encode(url)
    assert lnurl.startswith("LNURL1")
    assert lnurl_decode(lnurl) == url


def test_lightning_address_serves_the_pay_request(client: TestClient):
    data = client.get(f"/.well-known/lnurlp/{settings.username}").json()
    assert data["tag"] == "payRequest"
    assert data["withdrawLink"] == "http://testserver/w"
    assert f'["text/identifier", "{settings.username}@testserver"]' in data["metadata"]


def test_lightning_address_rejects_unknown_username(client: TestClient):
    data = client.get("/.well-known/lnurlp/nobody").json()
    assert data["status"] == "ERROR"


def test_mint_address_combines_withdraw_and_node_info(client: TestClient, node):
    data = client.get(f"/.well-known/lnurlw/{settings.username}").json()
    assert data["tag"] == "withdrawRequest"
    assert data["callback"] == "http://testserver/w"
    assert data["payLink"] == f"http://testserver/.well-known/lnurlp/{settings.username}"
    assert data["minWithdrawable"] == settings.min_mint_msat
    assert data["maxWithdrawable"] == settings.max_sendable_msat
    assert data["nodeAlias"] == "fakenode"
    assert data["nodeUri"] == f"{node.pubkey}@127.0.0.1:9735"
    assert data["nodeColor"] == "#3399ff"
    assert data["nodeCapacityMsat"] == 750_000_000
    assert data["nodeNumChannels"] == 3
    assert data["nodeNumPeers"] == 5
    assert data["mintPubkey"] == node.pubkey
    # theoretical/informational only - no real note k1 backs this address
    assert "k1" not in data


def test_mint_address_max_withdrawable_accounts_for_mint_fee(client: TestClient, node, monkeypatch):
    # maxWithdrawable must reflect what a note can actually net, not the raw
    # MAX_SENDABLE_MSAT setting - paying the full max still has the
    # configured fee withheld (see router.max_mintable_msat), the same
    # fee-aware treatment the LUD-16 address's own minSendable already gets on the floor
    monkeypatch.setattr(settings, "base_fee_msat", 1000)
    data = client.get(f"/.well-known/lnurlw/{settings.username}").json()
    assert data["maxWithdrawable"] == settings.max_sendable_msat - 1000


def test_mint_address_rejects_unknown_username(client: TestClient):
    data = client.get("/.well-known/lnurlw/nobody").json()
    assert data["status"] == "ERROR"


def test_mint_address_without_funding_source(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "fundingsource_backend", None)
    data = client.get(f"/.well-known/lnurlw/{settings.username}").json()
    assert data["tag"] == "withdrawRequest"
    assert data["payLink"] == f"http://testserver/.well-known/lnurlp/{settings.username}"
    assert "nodeAlias" not in data
    assert "mintPubkey" not in data


def test_index_shows_capacity_and_mint_limits(client: TestClient, node):
    response = client.get("/")
    assert "750,000 sats" in response.text
    assert f"{settings.min_mint_msat // 1000:,} sats" in response.text
    # fee-free test settings (see conftest.py) - the fee-adjusted ceiling
    # equals the raw setting here, the adjustment itself is exercised below
    assert f"{settings.max_sendable_msat // 1000:,} sats" in response.text


def test_index_max_amount_accounts_for_mint_fee(client: TestClient, node, monkeypatch):
    monkeypatch.setattr(settings, "base_fee_msat", 1000)
    response = client.get("/")
    assert f"{(settings.max_sendable_msat - 1000) // 1000:,} sats" in response.text
