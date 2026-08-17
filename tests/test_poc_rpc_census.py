"""PoC B1 (2026-08-17 review): precise per-endpoint funding-source RPC census.

Candidate claim (hunters' P2): unauthenticated endpoints amplify into
funding-source RPCs with no caching. The hunter verification of this failed
as written at one assertion ("/w on an outstanding note made no getinfo
call" was observed once as 2 total instead of 3, then flaked green), so
this file re-measures everything from scratch with counters on all eight
node RPCs and asserts exact per-request deltas.

All local/safe: FakeNode + TestClient + throwaway db.
"""

from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

import lnurl_mint.frontend as frontend_module
import lnurl_mint.router as router_module
import lnurl_mint.signing as signing_module
from lnurl_mint.config import settings
from lnurl_mint.db import notes
from tests.conftest import FakeNode, fake_invoice, fresh_secret

RPC_NAMES = (
    "create_invoice",
    "is_invoice_settled",
    "invoice_preimage",
    "pay_invoice",
    "is_payment_complete",
    "payment_preimage",
    "fetch_node_info",
    "sign_message",
)


class RpcCensus:
    """Wraps every FakeNode RPC method with a counter; deltas() reports
    per-RPC counts since the last snapshot()."""

    def __init__(self, node: FakeNode, monkeypatch: pytest.MonkeyPatch) -> None:
        self.counts = dict.fromkeys(RPC_NAMES, 0)
        self._snapshot = dict(self.counts)
        for name in RPC_NAMES:
            original = getattr(node, name)

            async def counting(*args, _original=original, _name=name, **kwargs):
                self.counts[_name] += 1
                return await _original(*args, **kwargs)

            for module in (router_module, frontend_module, signing_module):
                if getattr(module, name, None) is not None and name in module.__dict__:
                    monkeypatch.setattr(module, name, counting)

    def deltas(self) -> dict[str, int]:
        delta = {name: self.counts[name] - self._snapshot[name] for name in RPC_NAMES}
        self._snapshot = dict(self.counts)
        return {name: n for name, n in delta.items() if n}


@pytest.fixture
def census(node: FakeNode, monkeypatch: pytest.MonkeyPatch) -> RpcCensus:
    # /verify must be served at all for its RPC cost to be exercised -
    # VERIFY_ENABLED=false (the test-env default) 404s it since the review
    monkeypatch.setattr(settings, "verify_enabled", True)
    return RpcCensus(node, monkeypatch)


def test_frontend_index_one_getinfo_per_request(client: TestClient, census: RpcCensus):
    # GET / renders the node table live on every request - no caching.
    for _ in range(3):
        assert client.get("/").status_code == 200
        assert census.deltas() == {"fetch_node_info": 1}
    # static asset: no RPC at all
    assert client.get("/favicon.svg").status_code == 200
    assert census.deltas() == {}


def test_pay_endpoints_no_rpc(client: TestClient, census: RpcCensus):
    # the LUD-16 alias (this mint's only payRequest entry point) is pure
    # settings arithmetic - zero RPCs.
    assert client.get("/.well-known/lnurlp/mint").status_code == 200
    assert census.deltas() == {}


def test_pay_callback_one_create_invoice_per_call_stateful_bloat(client: TestClient, census: RpcCensus):
    # Every /p/cb mints a fresh invoice on the node (1 RPC) and a row in the
    # local mints table - both persist forever if never paid. On a real
    # lnd/cln backend the invoice persists in the *node's* database too,
    # making this a stateful bloat vector against the funding source, not
    # just a CPU one.
    prs = set()
    for _ in range(3):
        resp = client.get("/p/cb?amount=50000")
        assert resp.status_code == 200
        prs.add(resp.json()["pr"])
        assert census.deltas() == {"create_invoice": 1}
    assert len(prs) == 3
    # three unpaid pending mints now sit in the local db (and would sit in
    # the node's invoice db on a real backend)
    import bolt11

    pending = [notes.pending_mint(bolt11.decode(pr).payment_hash) for pr in prs]
    assert all(amount == 50_000 for amount in pending)


def test_withdraw_info_census(client: TestClient, node: FakeNode, mint_note, census: RpcCensus):
    # case 1: first /w on a settled-but-not-yet-materialized mint k1:
    # 1 is_invoice_settled (lazy settlement) + 1 fetch_node_info (mint_pubkey)
    k1 = mint_note(10_000)
    census.deltas()  # discard /p/cb's own create_invoice
    assert client.get(f"/w?k1={k1}").status_code == 200
    assert census.deltas() == {"is_invoice_settled": 1, "fetch_node_info": 1}

    # case 2: same note again - now materialized locally, so the settlement
    # probe is gone, but mint_pubkey (signing.py L30-48) has NO cache:
    # still exactly 1 getinfo-class RPC per request, forever.
    for _ in range(3):
        assert client.get(f"/w?k1={k1}").status_code == 200
        assert census.deltas() == {"fetch_node_info": 1}

    # case 3: /w on an *unsettled* pending mint's k1 (the preimage is only
    # learnable by the payer, but the shape matters for the census): the
    # lazy settlement probe fires on EVERY request - no negative caching -
    # and the ERROR short-circuits before mint_pubkey. (Lnurl routes answer
    # errors as HTTP 200 + {"status": "ERROR", ...} - see error_handler.py.)
    resp = client.get("/p/cb?amount=50000")
    assert resp.status_code == 200
    unsettled_k1 = node.last_preimage.hex()
    census.deltas()  # discard /p/cb's own create_invoice
    for _ in range(3):
        r = client.get(f"/w?k1={unsettled_k1}")
        assert r.json() == {"status": "ERROR", "reason": "Unknown note."}
        assert census.deltas() == {"is_invoice_settled": 1}

    # case 4: unknown k1 (valid hex, never issued) - no RPC, pure local ERROR
    random_k1, _ = fresh_secret()
    r = client.get(f"/w?k1={random_k1}")
    assert r.json() == {"status": "ERROR", "reason": "Unknown note."}
    assert census.deltas() == {}

    # case 5: spent note - answered from the local spent flag, no RPC
    spent_k1 = mint_note(10_000)
    _, h = fresh_secret()
    assert client.get(f"/w/cb?k1={spent_k1}&h={h}").json()["status"] == "OK"
    census.deltas()  # discard the rotate's own sign_message
    r = client.get(f"/w?k1={spent_k1}")
    assert r.json() == {"status": "ERROR", "reason": "Note already spent."}
    assert census.deltas() == {}


def test_verify_census_unsettled_mint_polls_forever(client: TestClient, node: FakeNode, census: RpcCensus):
    # an unpaid mint invoice polled via /verify: 1 is_invoice_settled RPC
    # per poll, forever - no negative caching, and (checked live against a
    # real node) this continues even after the invoice would have expired
    # node-side.
    client.get("/p/cb?amount=50000")
    ph = sha256(node.last_preimage).hexdigest()
    census.deltas()  # discard create_invoice
    for _ in range(5):
        r = client.get(f"/verify/{ph}")
        assert r.json()["settled"] is False
        assert census.deltas() == {"is_invoice_settled": 1}


def test_verify_census_settled_mint_preimage_per_poll(client: TestClient, node: FakeNode, census: RpcCensus):
    # once settled, the settlement probe short-circuits on the local minted
    # flag - but the preimage is "fetched live, never cached" by design, so
    # every poll of a settled mint still costs 1 invoice_preimage RPC,
    # forever.
    client.get("/p/cb?amount=50000")
    ph = sha256(node.last_preimage).hexdigest()
    census.deltas()
    node.settled.add(ph)
    # first poll: settles locally (1 is_invoice_settled) + fetches preimage
    r = client.get(f"/verify/{ph}")
    assert r.json()["settled"] is True
    assert r.json()["preimage"] == node.last_preimage.hex()
    assert census.deltas() == {"is_invoice_settled": 1, "invoice_preimage": 1}
    # subsequent polls: preimage only
    for _ in range(3):
        assert client.get(f"/verify/{ph}").json()["settled"] is True
        assert census.deltas() == {"invoice_preimage": 1}


def test_verify_census_melt_direction(client: TestClient, node: FakeNode, mint_note, census: RpcCensus):
    k1 = mint_note(10_000)
    invoice = fake_invoice(10_000)
    melt_ph = __import__("bolt11").decode(invoice).payment_hash
    assert client.get(f"/w/cb?k1={k1}&pr={invoice}").json()["status"] == "OK"
    census.deltas()  # discard the melt's own pay_invoice

    # melt in-flight (not complete): 1 is_payment_complete per poll
    for _ in range(2):
        r = client.get(f"/verify/{melt_ph}")
        assert r.json()["settled"] is False
        assert census.deltas() == {"is_payment_complete": 1}

    # melt complete: 1 is_payment_complete + 1 payment_preimage per poll -
    # neither is ever cached
    node.payment_actually_completed = True
    for _ in range(2):
        r = client.get(f"/verify/{melt_ph}")
        assert r.json()["settled"] is True
        assert r.json()["preimage"] is not None
        assert census.deltas() == {"is_payment_complete": 1, "payment_preimage": 1}


def test_withdraw_callback_signing_rpcs(client: TestClient, node: FakeNode, mint_note, census: RpcCensus):
    def minted_materialized_note(amount_msat: int) -> str:
        """A settled note already materialized locally (via /w), with all
        of its mint/resolve RPCs discarded from the census - so the deltas
        below measure the /w/cb call alone."""
        k1 = mint_note(amount_msat)
        assert client.get(f"/w?k1={k1}").status_code == 200
        census.deltas()
        return k1

    # rotate: 1 sign_message per request, never cached
    k1 = minted_materialized_note(10_000)
    _, h = fresh_secret()
    assert client.get(f"/w/cb?k1={k1}&h={h}").json()["status"] == "OK"
    assert census.deltas() == {"sign_message": 1}

    # split: 2 sign_message per request (one per new note)
    k1 = minted_materialized_note(10_000)
    _, h = fresh_secret()
    _, h2 = fresh_secret()
    assert client.get(f"/w/cb?k1={k1}&h={h}&h2={h2}&amount=4000").json()["status"] == "OK"
    assert census.deltas() == {"sign_message": 2}

    # merge: 1 sign_message per request (regardless of input count)
    k1a, k1b = minted_materialized_note(10_000), minted_materialized_note(10_000)
    _, h = fresh_secret()
    assert client.get(f"/w/cb?k1={k1a}&k1={k1b}&h={h}").json()["status"] == "OK"
    assert census.deltas() == {"sign_message": 1}

    # melt: 1 pay_invoice (background task, runs within the TestClient
    # request) + no signing; note value crosses the Lightning network
    k1 = minted_materialized_note(10_000)
    assert client.get(f"/w/cb?k1={k1}&pr={fake_invoice(10_000)}").json()["status"] == "OK"
    assert census.deltas() == {"pay_invoice": 1}
