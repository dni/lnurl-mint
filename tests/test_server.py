import logging
import time

from fastapi.testclient import TestClient

import lnurl_mint.server as server_module
from lnurl_mint.config import settings
from lnurl_mint.node import NodeInfo
from lnurl_mint.server import app


def test_startup_disables_the_uvicorn_access_logger():
    # LUD-25: a bearer note's k1 sits in the query string of /w and
    # /w/cb for as long as it's held, so the default per-request
    # access log (which includes the full query string) would otherwise
    # write it to disk on every lookup or spend - see server.py's lifespan
    # for why this must happen there and not at import time.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = False
    with TestClient(app):
        pass
    assert access_logger.disabled is True


def test_startup_warns_when_no_funding_source_configured(monkeypatch, caplog):
    monkeypatch.setattr(settings, "fundingsource_backend", None)
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            pass
    assert any("No funding source configured" in r.message for r in caplog.records)


def test_startup_warns_when_funding_source_unreachable(monkeypatch, caplog):
    async def _broken_fetch_node_info(config):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(server_module, "fetch_node_info", _broken_fetch_node_info)
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            pass
    assert any("unreachable at startup" in r.message and "connection refused" in r.message for r in caplog.records)


def test_startup_logs_success_when_funding_source_reachable(monkeypatch, caplog):
    async def _fake_fetch_node_info(config):
        return NodeInfo(alias="fakenode", uri="02abcdef@127.0.0.1:9735", num_channels=1, num_peers=1)

    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(server_module, "fetch_node_info", _fake_fetch_node_info)
    with caplog.at_level(logging.INFO):
        with TestClient(app):
            pass
    assert any("Connected to lnd funding source" in r.message and "fakenode" in r.message for r in caplog.records)


def test_background_monitor_warns_when_funding_source_breaks_after_boot(monkeypatch, caplog):
    # regression for issue #2: a mint whose funding source went bad *after*
    # a healthy boot used to keep accepting melts it couldn't fulfill, with
    # nothing in the logs pointing at why - the one-shot boot check alone
    # can never catch a problem that develops later
    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(settings, "funding_source_health_check_interval_seconds", 0.01)

    calls = {"n": 0}

    async def _flaky_fetch_node_info(config):
        calls["n"] += 1
        if calls["n"] == 1:  # the boot check itself - healthy
            return NodeInfo(alias="fakenode", uri="02abcdef@127.0.0.1:9735", num_channels=1, num_peers=1)
        raise ConnectionError("connection refused")  # every monitor tick after

    monkeypatch.setattr(server_module, "fetch_node_info", _flaky_fetch_node_info)
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            time.sleep(0.1)  # let a few monitor ticks run
    assert any("lnd funding source became unreachable" in r.message for r in caplog.records)


def test_background_monitor_logs_recovery(monkeypatch, caplog):
    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(settings, "funding_source_health_check_interval_seconds", 0.01)

    calls = {"n": 0}

    async def _flaky_fetch_node_info(config):
        calls["n"] += 1
        if calls["n"] <= 2:  # boot check + first monitor tick - unreachable
            raise ConnectionError("connection refused")
        return NodeInfo(alias="fakenode", uri="02abcdef@127.0.0.1:9735", num_channels=1, num_peers=1)

    monkeypatch.setattr(server_module, "fetch_node_info", _flaky_fetch_node_info)
    with caplog.at_level(logging.INFO):
        with TestClient(app):
            time.sleep(0.1)
    assert any("lnd funding source is reachable again" in r.message for r in caplog.records)


def test_background_monitor_does_not_repeat_the_same_state(monkeypatch, caplog):
    # healthy at boot, breaks once, then stays broken for several more
    # ticks - "became unreachable" must be logged exactly once (the
    # transition), not once per tick for as long as it stays down
    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    monkeypatch.setattr(settings, "funding_source_health_check_interval_seconds", 0.01)

    calls = {"n": 0}

    async def _fetch_node_info(config):
        calls["n"] += 1
        if calls["n"] == 1:  # the boot check - healthy
            return NodeInfo(alias="fakenode", uri="02abcdef@127.0.0.1:9735", num_channels=1, num_peers=1)
        raise ConnectionError("connection refused")  # every monitor tick after

    monkeypatch.setattr(server_module, "fetch_node_info", _fetch_node_info)
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            time.sleep(0.1)  # several ticks worth, all still unreachable
    unreachable_warnings = [r for r in caplog.records if "became unreachable" in r.message]
    assert len(unreachable_warnings) == 1
    assert calls["n"] > 2  # confirms multiple ticks actually ran, not just one
