import pytest
from pydantic import ValidationError

from lnurl_mint.config import Settings


def test_base_url_is_required(monkeypatch):
    # unlike every other setting, base_url has no default and is never
    # derived from a request's own Host header (see config.py) - an
    # operator who forgets it should get a clear failure at startup, not a
    # silently Host-header-trusting mint
    monkeypatch.delenv("BASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_previous_mint_pubkeys_are_normalised(monkeypatch):
    monkeypatch.setenv("MINT_PREVIOUS_PUBKEYS", f" 02{'AB' * 32},03{'cd' * 32} ")
    configured = Settings()
    assert configured.previous_mint_pubkeys() == [f"02{'ab' * 32}", f"03{'cd' * 32}"]


@pytest.mark.parametrize(
    "value",
    [
        "02deadbeef",
        f"04{'ab' * 32}",
        f"02{'ab' * 32},02{'ab' * 32}",
    ],
)
def test_previous_mint_pubkeys_reject_invalid_or_duplicate_keys(monkeypatch, value):
    monkeypatch.setenv("MINT_PREVIOUS_PUBKEYS", value)
    with pytest.raises(ValidationError):
        Settings()
