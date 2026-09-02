import asyncio
import json
import logging

import httpx
import pytest

import lnurl_mint.node as node_module
from lnurl_mint.node import (
    LightningBackendConfig,
    NodeInfo,
    _cln_pay_failure_reason,
    _fetch_node_info_cln,
    _fetch_node_info_lnd,
    _is_payment_complete_cln,
    _is_payment_complete_lnd,
    _lnd_failure_reason,
    cached_fetch_node_info,
)

PAYMENT_HASH = "11" * 32
LND_CONFIG = LightningBackendConfig(backend="lnd", url="https://lnd.example", macaroon="deadbeef")
CLN_CONFIG = LightningBackendConfig(backend="cln", url="https://cln.example", rune="deadbeef")


_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(response: httpx.Response):
    """Drop-in replacement for httpx.AsyncClient that always answers with
    `response` instead of a real connection - lets is_payment_complete's
    backend implementations be exercised against a fake lnd/cln response,
    without a real node, including the streamed lnd case."""

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(lambda request: response), **kwargs)

    return factory


def _run(coro):
    return asyncio.run(coro)


def _mock_async_client_by_path(responses: dict[str, httpx.Response]):
    """Like _mock_async_client, but dispatches on request path - for
    fetch_node_info's two-call (getinfo, then a public-graph capacity
    lookup) shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        return responses[request.url.path]

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(handler), **kwargs)

    return factory


def test_lnd_node_info_reports_public_graph_capacity_in_msat(monkeypatch):
    # GetNodeInfo (self-lookup, /v1/graph/node/{pubkey}), not ListChannels -
    # this must read the same total_capacity a stranger could already see
    # from this node's own announced channels, never its private balances.
    # lnd reports total_capacity in sats, converted here to keep
    # NodeInfo.capacity msat-denominated like everything else.
    responses = {
        "/v1/getinfo": httpx.Response(200, json={"alias": "n", "identity_pubkey": "abc"}),
        "/v1/graph/node/abc": httpx.Response(200, json={"num_channels": 2, "total_capacity": "150000"}),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client_by_path(responses))
    info = _run(_fetch_node_info_lnd(LND_CONFIG.url, "deadbeef", LND_CONFIG))
    assert info.capacity == 150_000_000


def test_lnd_node_info_capacity_failure_is_swallowed(monkeypatch, caplog):
    responses = {
        "/v1/getinfo": httpx.Response(200, json={"alias": "n", "identity_pubkey": "abc"}),
        "/v1/graph/node/abc": httpx.Response(404, text="node not found in graph"),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client_by_path(responses))
    with caplog.at_level(logging.WARNING):
        info = _run(_fetch_node_info_lnd(LND_CONFIG.url, "deadbeef", LND_CONFIG))
    assert info.alias == "n"
    assert info.capacity == 0
    # a failure must be diagnosable, not indistinguishable from "genuinely 0"
    assert any("could not fetch capacity" in r.message for r in caplog.records)


def test_cln_node_info_reports_public_graph_capacity_in_msat(monkeypatch):
    # listchannels source=<our id> (public gossip), not listfunds - a
    # single public channel appears once per direction, sharing the same
    # capacity, so the duplicate must not double-count it.
    responses = {
        "/v1/getinfo": httpx.Response(200, json={"id": "abc", "alias": "n"}),
        "/v1/listchannels": httpx.Response(
            200,
            json={
                "channels": [
                    {"short_channel_id": "1x1x0", "amount_msat": 100_000_000},
                    {"short_channel_id": "1x1x0", "amount_msat": 100_000_000},
                    {"short_channel_id": "2x2x0", "amount_msat": 50_000_000},
                ]
            },
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client_by_path(responses))
    info = _run(_fetch_node_info_cln(CLN_CONFIG.url, "deadbeef", CLN_CONFIG))
    assert info.capacity == 150_000_000


def test_cln_node_info_reports_every_advertised_address(monkeypatch):
    # a node set up for Tor as well as clearnet reports more than one entry
    # in `address` - every one of them is a valid way in, not just the
    # first, and `uri` (the single primary connect string other callers key
    # off) must stay == uris[0].
    responses = {
        "/v1/getinfo": httpx.Response(
            200,
            json={
                "id": "abc",
                "alias": "n",
                "address": [
                    {"type": "ipv4", "address": "1.2.3.4", "port": 9735},
                    {"type": "torv3", "address": "xyz.onion", "port": 9735},
                ],
            },
        ),
        "/v1/listchannels": httpx.Response(200, json={"channels": []}),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client_by_path(responses))
    info = _run(_fetch_node_info_cln(CLN_CONFIG.url, "deadbeef", CLN_CONFIG))
    assert info.uris == ["abc@1.2.3.4:9735", "abc@xyz.onion:9735"]
    assert info.uri == "abc@1.2.3.4:9735"


def test_cln_node_info_capacity_failure_is_swallowed(monkeypatch, caplog):
    responses = {
        "/v1/getinfo": httpx.Response(200, json={"id": "abc", "alias": "n"}),
        "/v1/listchannels": httpx.Response(500, text="rune error"),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client_by_path(responses))
    with caplog.at_level(logging.WARNING):
        info = _run(_fetch_node_info_cln(CLN_CONFIG.url, "deadbeef", CLN_CONFIG))
    assert info.alias == "n"
    assert info.capacity == 0
    # a rune baked before `listchannels` was added to the required set (see
    # README) is exactly this failure mode - the warning is what makes it
    # diagnosable instead of looking like "this node genuinely has 0"
    assert any("could not fetch capacity" in r.message for r in caplog.records)


class _FakeMonotonic:
    """Stands in for node.time so cached_fetch_node_info's TTL check can be
    driven deterministically (advance `.now`) instead of actually sleeping
    an hour - only replaces the `time` name inside node.py's own namespace,
    the real time module elsewhere is untouched."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def test_cached_fetch_node_info_reuses_within_the_ttl(monkeypatch):
    monkeypatch.setattr(node_module, "_node_info_cache", None)
    fake_time = _FakeMonotonic()
    monkeypatch.setattr(node_module, "time", fake_time)
    calls = {"n": 0}

    async def fake_fetch(config: LightningBackendConfig) -> NodeInfo:
        calls["n"] += 1
        return NodeInfo(alias=f"call-{calls['n']}")

    monkeypatch.setattr(node_module, "fetch_node_info", fake_fetch)

    first = _run(cached_fetch_node_info(LND_CONFIG))
    second = _run(cached_fetch_node_info(LND_CONFIG))
    assert first.alias == "call-1"
    assert second.alias == "call-1"  # served from cache, not refetched
    assert calls["n"] == 1

    fake_time.now += node_module._NODE_INFO_CACHE_TTL_SECONDS + 1
    third = _run(cached_fetch_node_info(LND_CONFIG))
    assert third.alias == "call-2"
    assert calls["n"] == 2


def test_cached_fetch_node_info_does_not_cache_a_failure(monkeypatch):
    monkeypatch.setattr(node_module, "_node_info_cache", None)
    calls = {"n": 0}

    async def flaky_fetch(config: LightningBackendConfig) -> NodeInfo:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("funding source unreachable")
        return NodeInfo(alias="recovered")

    monkeypatch.setattr(node_module, "fetch_node_info", flaky_fetch)

    with pytest.raises(ConnectionError):
        _run(cached_fetch_node_info(LND_CONFIG))

    # the failed attempt above must not have poisoned the cache - the very
    # next call retries live and succeeds, rather than staying "unreachable"
    info = _run(cached_fetch_node_info(LND_CONFIG))
    assert info.alias == "recovered"
    assert calls["n"] == 2


def test_lnd_payment_complete_reports_true_for_succeeded(monkeypatch):
    response = httpx.Response(200, text=json.dumps({"result": {"status": "SUCCEEDED"}}) + "\n")
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    assert _run(_is_payment_complete_lnd(PAYMENT_HASH, LND_CONFIG.url, "deadbeef", LND_CONFIG)) is True


def test_lnd_payment_complete_reports_false_for_failed(monkeypatch):
    response = httpx.Response(200, text=json.dumps({"result": {"status": "FAILED"}}) + "\n")
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    assert _run(_is_payment_complete_lnd(PAYMENT_HASH, LND_CONFIG.url, "deadbeef", LND_CONFIG)) is False


def test_lnd_payment_complete_reports_false_when_never_attempted(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(httpx.Response(404)))
    assert _run(_is_payment_complete_lnd(PAYMENT_HASH, LND_CONFIG.url, "deadbeef", LND_CONFIG)) is False


def test_lnd_payment_complete_raises_rather_than_reports_false_for_in_flight(monkeypatch):
    # regression: a hodl invoice held open by a malicious payee can make
    # lnd report IN_FLIGHT indefinitely - this must never be mistaken for
    # "confirmed not paid" (see _is_payment_complete_lnd's docstring), or a
    # melt into such an invoice could be restored and re-melted while the
    # original payment is still separately claimable - a double payout
    response = httpx.Response(200, text=json.dumps({"result": {"status": "IN_FLIGHT"}}) + "\n")
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    with pytest.raises(ValueError):
        _run(_is_payment_complete_lnd(PAYMENT_HASH, LND_CONFIG.url, "deadbeef", LND_CONFIG))


def test_cln_payment_complete_reports_true_for_complete(monkeypatch):
    response = httpx.Response(200, json={"pays": [{"status": "complete"}]})
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    assert _run(_is_payment_complete_cln(PAYMENT_HASH, CLN_CONFIG.url, "deadbeef", CLN_CONFIG)) is True


def test_cln_payment_complete_reports_false_for_failed(monkeypatch):
    response = httpx.Response(200, json={"pays": [{"status": "failed"}]})
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    assert _run(_is_payment_complete_cln(PAYMENT_HASH, CLN_CONFIG.url, "deadbeef", CLN_CONFIG)) is False


def test_cln_payment_complete_reports_false_when_never_attempted(monkeypatch):
    response = httpx.Response(200, json={"pays": []})
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    assert _run(_is_payment_complete_cln(PAYMENT_HASH, CLN_CONFIG.url, "deadbeef", CLN_CONFIG)) is False


def test_cln_payment_complete_raises_rather_than_reports_false_for_pending(monkeypatch):
    # regression: xpay giving up (retry_for exhausted) does not guarantee
    # no HTLC remains outstanding - a malicious payee holding a hodl
    # invoice open can keep a "pending" listpays entry indefinitely (see
    # _is_payment_complete_cln's docstring)
    response = httpx.Response(200, json={"pays": [{"status": "pending"}]})
    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(response))
    with pytest.raises(ValueError):
        _run(_is_payment_complete_cln(PAYMENT_HASH, CLN_CONFIG.url, "deadbeef", CLN_CONFIG))


def test_cln_pay_failure_reason_maps_known_codes():
    res = httpx.Response(500, json={"code": 205, "message": "Destination unreachable"})
    assert _cln_pay_failure_reason(res) == "Could not find a route to pay this invoice."


def test_cln_pay_failure_reason_falls_back_to_clns_own_message_for_unmapped_codes():
    res = httpx.Response(500, json={"code": 999, "message": "Some other cln error"})
    assert _cln_pay_failure_reason(res) == "Some other cln error"


def test_cln_pay_failure_reason_falls_back_to_status_code_for_an_unparseable_body():
    res = httpx.Response(500, content=b"not json")
    assert _cln_pay_failure_reason(res) == "Payment failed (500)."


def test_lnd_failure_reason_maps_known_reasons():
    assert _lnd_failure_reason("FAILURE_REASON_NO_ROUTE") == "Could not find a route to pay this invoice."


def test_lnd_failure_reason_falls_back_to_the_raw_reason_for_unmapped_ones():
    assert _lnd_failure_reason("FAILURE_REASON_ERROR") == "Payment failed: FAILURE_REASON_ERROR."
