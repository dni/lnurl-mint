from fastapi.testclient import TestClient

from lnurl_mint.config import settings

ONION = "http://abcdefghijklmnop1234567890abcdefghijklmnop1234567890abcdefgh.onion"


def test_public_base_url_prefers_onion_for_matching_host(monkeypatch):
    monkeypatch.setattr(settings, "onion_url", ONION)
    # a conflicting clearnet base_url is also configured, so this only
    # passes if the onion branch actually wins on its own merits (matching
    # host), not merely by being the only/coincidentally-equal value
    monkeypatch.setattr(settings, "base_url", "https://mint.example")
    assert settings.public_base_url(f"{ONION}/") == ONION


def test_public_base_url_ignores_onion_for_other_hosts(monkeypatch):
    monkeypatch.setattr(settings, "onion_url", ONION)
    monkeypatch.setattr(settings, "base_url", "https://mint.example")
    assert settings.public_base_url("http://clearnet.example/") == "https://mint.example"


def test_public_base_url_ignores_request_when_onion_unset(monkeypatch):
    monkeypatch.setattr(settings, "onion_url", None)
    monkeypatch.setattr(settings, "base_url", "https://mint.example")
    # base_url is required and never derived from the request's own Host
    # header (see config.py) - a request for a completely different host
    # still gets the fixed base_url
    assert settings.public_base_url("http://clearnet.example/") == "https://mint.example"


def test_public_base_url_still_prefers_base_url_over_request(monkeypatch):
    monkeypatch.setattr(settings, "onion_url", ONION)
    monkeypatch.setattr(settings, "base_url", "https://mint.example")
    # a clearnet request when both base_url and onion_url are configured
    # gets the fixed base_url, not the request's own host
    assert settings.public_base_url("http://clearnet.example/") == "https://mint.example"


def test_pay_response_uses_onion_url_when_reached_via_onion(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "onion_url", ONION)
    response = client.get("/p", headers={"host": "abcdefghijklmnop1234567890abcdefghijklmnop1234567890abcdefgh.onion"})
    data = response.json()
    assert data["callback"] == f"{ONION}/p/cb"
    assert data["withdrawLink"] == f"{ONION}/w"


def test_index_shows_tor_section_when_configured(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "onion_url", ONION)
    response = client.get("/")
    assert "Also via Tor" in response.text
    assert f"{settings.username}@abcdefghijklmnop1234567890abcdefghijklmnop1234567890abcdefgh.onion" in response.text


def test_index_hides_tor_section_when_not_configured(client: TestClient):
    response = client.get("/")
    assert "Also via Tor" not in response.text


def test_index_omits_redundant_tor_section_when_already_on_onion(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "onion_url", ONION)
    response = client.get("/", headers={"host": "abcdefghijklmnop1234567890abcdefghijklmnop1234567890abcdefgh.onion"})
    assert "Also via Tor" not in response.text
