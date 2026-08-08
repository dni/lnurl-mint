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
