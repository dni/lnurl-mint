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
    # the QR/copy string is the bech32 LNURL of the mint's payRequest
    assert lnurl_encode("http://testserver/pay") in response.text


def test_index_shows_node_info(client: TestClient):
    response = client.get("/")
    assert "fakenode" in response.text
    assert "02abcdef@127.0.0.1:9735" in response.text


def test_index_without_funding_source(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "fundingsource_backend", None)
    response = client.get("/")
    assert response.status_code == 200
    assert "No funding source configured." in response.text


def test_base_url_setting_overrides_request_url(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://mint.example")
    response = client.get("/")
    assert lnurl_encode("https://mint.example/pay") in response.text
    assert f"{settings.username}@mint.example" in response.text

    pay = client.get("/pay").json()
    assert pay["callback"] == "https://mint.example/pay/cb"
    assert pay["withdrawLink"] == "https://mint.example/withdraw"
    assert f"{settings.username}@mint.example" in pay["metadata"]


def test_lnurl_encode_roundtrip():
    url = "https://mint.example/pay"
    lnurl = lnurl_encode(url)
    assert lnurl.startswith("LNURL1")
    assert lnurl_decode(lnurl) == url


def test_lightning_address_serves_the_pay_request(client: TestClient):
    data = client.get(f"/.well-known/lnurlp/{settings.username}").json()
    assert data["tag"] == "payRequest"
    assert data["withdrawLink"] == "http://testserver/withdraw"
    assert f'["text/identifier", "{settings.username}@testserver"]' in data["metadata"]


def test_lightning_address_rejects_unknown_username(client: TestClient):
    data = client.get("/.well-known/lnurlp/nobody").json()
    assert data["status"] == "ERROR"
