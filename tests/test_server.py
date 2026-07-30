import logging

from fastapi.testclient import TestClient

from lnurl_mint.server import app


def test_startup_disables_the_uvicorn_access_logger():
    # LUD-XX: a bearer note's k1 sits in the query string of /w and
    # /w/cb for as long as it's held, so the default per-request
    # access log (which includes the full query string) would otherwise
    # write it to disk on every lookup or spend - see server.py's lifespan
    # for why this must happen there and not at import time.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = False
    with TestClient(app):
        pass
    assert access_logger.disabled is True
