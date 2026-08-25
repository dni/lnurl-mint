"""Regression tests from the surface lane of the 2026-08-17 security review
(originally PoCs, flipped to pin the fixed behavior).

- P3/F-1: rotate with h == a pending mint's payment_hash is rejected by the
  swap guard; the victim's mint materializes normally. (The exhaustive
  variant matrix - split h/h2, merge, settled-mint ids - lives in
  test_poc_a1_collision_griefing.py; this file keeps the canonical case.)
- P1/F-3: FIXED (LUD-25 comment protection) - /verify used to hand a
  settled mint's preimage to anyone holding the payment_hash when
  VERIFY_ENABLED=true, and that preimage was the note's entire spend
  secret. Now SERVICE refuses verify outright for any mint that skipped
  `comment` (NoteStore.mint_uses_comment), and for one that used it the
  disclosed preimage isn't the note's secret to begin with - see
  test_p1_verify_no_longer_hands_out_the_no_comment_fallback_secret and
  test_p1b_verify_is_harmless_once_comment_protection_is_used. The
  VERIFY_ENABLED off switch itself is still pinned in
  test_poc_verify_race.py.
- P2/F-5: unauthenticated endpoints amplify into funding-source RPCs with
  no caching (availability concern) - GET /'s getinfo is the flip this
  file's own comment anticipated: it now goes through
  node.cached_fetch_node_info (2026-08-18, a shared 1h in-process cache),
  so repeated requests within the TTL cost nothing further. mint_pubkey's
  getinfo (GET /w, signing.py) is deliberately still live/uncached, and
  /verify's settlement polling is unrelated to node info entirely - both
  remain exactly 1:1 with requests, unchanged.
- P6/F-4: fee_percent_ppm beyond the validated bound can no longer reach
  _min_sendable_msat through Settings at all, and the function's own
  iteration cap converts even a post-construction mutation into a raised
  error instead of a hang.
"""

import threading
from hashlib import sha256

from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from lnurl_mint.db import notes
from lnurl_mint.router import _min_sendable_msat
from tests.conftest import FakeNode, fresh_secret


def test_p3_rotate_onto_pending_mint_is_rejected(client: TestClient, node: FakeNode, mint_note):
    # attacker owns a note and knows a victim's pending mint payment_hash
    # (learnable from the victim's invoice pr, which embeds it)
    attacker_k1 = mint_note(10_000)

    # victim requests a mint invoice but has not paid it yet
    client.get("/p/cb?amount=50000")
    victim_preimage = node.last_preimage
    victim_ph = sha256(victim_preimage).hexdigest()
    assert notes.pending_mint(victim_ph) is not None

    # the squat is rejected atomically - nothing planted, nothing burned
    resp = client.get(f"/w/cb?k1={attacker_k1}&h={victim_ph}")
    assert resp.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}, resp.text
    assert notes.note_amount(victim_ph) is None
    attacker_id = sha256(bytes.fromhex(attacker_k1)).hexdigest()
    assert notes.note_amount(attacker_id) == 10_000

    # victim pays -> their mint materializes for the full amount
    node.settled.add(victim_ph)
    body = client.get(f"/w?k1={victim_preimage.hex()}").json()
    assert body.get("tag") == "withdrawRequest", body
    assert body["maxWithdrawable"] == 50_000, body
    assert notes.mint_settled(victim_ph) is True


def test_p1_verify_no_longer_hands_out_the_no_comment_fallback_secret(client: TestClient, node: FakeNode, monkeypatch):
    # FIXED by LUD-25 comment protection (router.get_pay_callback /
    # NoteStore.mint_uses_comment): a mint that skipped `comment` falls back
    # to k1=preimage, and that preimage IS the note's entire spend secret -
    # so SERVICE now refuses verify for it outright, even with VERIFY_ENABLED
    # on, instead of handing it to anyone who ever saw the invoice.
    monkeypatch.setattr(settings, "verify_enabled", True)
    resp = client.get("/p/cb?amount=50000")
    assert "verify" not in resp.json()
    victim_preimage = node.last_preimage
    payment_hash = sha256(victim_preimage).hexdigest()
    node.settled.add(payment_hash)

    assert client.get(f"/verify/{payment_hash}").json() == {"status": "ERROR", "reason": "Not found"}

    # the victim's own preimage, learned the ordinary way (their own
    # Lightning payment), still redeems the note normally - the fallback
    # note itself is unaffected, only the remote-disclosure endpoint is closed
    _, victim_h = fresh_secret()
    rotate = client.get(f"/w/cb?k1={victim_preimage.hex()}&h={victim_h}")
    assert rotate.json()["status"] == "OK", rotate.text


def test_p1b_verify_is_harmless_once_comment_protection_is_used(client: TestClient, node: FakeNode, monkeypatch):
    # the complementary case: a WALLET that DOES use LUD-25 comment
    # protection gets verify served, but the disclosed preimage is no
    # longer the note's spend secret (the WALLET-held `secret` behind
    # `comment` is), so an observer stealing it from /verify gets nothing
    monkeypatch.setattr(settings, "verify_enabled", True)
    secret, comment = fresh_secret()
    resp = client.get(f"/p/cb?amount=50000&comment={comment}")
    assert resp.json().get("verify")
    victim_preimage = node.last_preimage
    payment_hash = sha256(victim_preimage).hexdigest()
    node.settled.add(payment_hash)

    stolen = client.get(f"/verify/{payment_hash}").json()
    assert stolen["settled"] is True
    assert stolen["preimage"] == victim_preimage.hex()

    # the stolen preimage redeems nothing - it was never the note's k1
    _, attacker_h = fresh_secret()
    rotate = client.get(f"/w/cb?k1={stolen['preimage']}&h={attacker_h}")
    assert rotate.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}

    # only the WALLET-held secret does
    _, victim_h = fresh_secret()
    rotate = client.get(f"/w/cb?k1={secret}&h={victim_h}")
    assert rotate.json()["status"] == "OK", rotate.text


def test_p2_rpc_amplification_getinfo_now_cached_mint_pubkey_still_not(
    client: TestClient, node: FakeNode, mint_note, monkeypatch
):
    # verify must be served at all for its RPC cost to be exercised
    monkeypatch.setattr(settings, "verify_enabled", True)
    calls = {"info": 0, "settled": 0}
    orig_info, orig_settled = node.fetch_node_info, node.is_invoice_settled

    async def counting_info(config):
        calls["info"] += 1
        return await orig_info(config)

    async def counting_settled(payment_hash, config):
        calls["settled"] += 1
        return await orig_settled(payment_hash, config)

    import lnurl_mint.node as node_module
    import lnurl_mint.router as router_module
    import lnurl_mint.signing as signing_module

    # node.cached_fetch_node_info (GET /, frontend.py) reaches the fake via
    # node.py's own module-global fetch_node_info name, not a per-module
    # import like the other RPCs here - see conftest.py's node fixture
    monkeypatch.setattr(node_module, "fetch_node_info", counting_info)
    monkeypatch.setattr(signing_module, "fetch_node_info", counting_info)
    monkeypatch.setattr(router_module, "is_invoice_settled", counting_settled)

    # GET / -> cached (node.cached_fetch_node_info, 1h TTL): only the first
    # of these two requests actually reaches the funding source
    client.get("/")
    client.get("/")
    assert calls["info"] == 1

    # GET /w on an outstanding note -> mint_pubkey (signing.py) -> a live
    # getinfo RPC, deliberately uncached, every single time
    k1 = mint_note(10_000)
    client.get(f"/w?k1={k1}")
    assert calls["info"] == 2
    client.get(f"/w?k1={k1}")
    assert calls["info"] == 3

    # an unsettled pending mint polled via /verify -> 1 RPC per poll, forever
    # (no negative caching even after the node-side invoice would expire) -
    # a different RPC entirely, unaffected by node-info caching. Comment
    # protection (LUD-25) is what gets verify served at all - see
    # router.get_pay_callback.
    _, comment = fresh_secret()
    client.get(f"/p/cb?amount=50000&comment={comment}")
    ph = sha256(node.last_preimage).hexdigest()
    settled_before = calls["settled"]
    for _ in range(5):
        r = client.get(f"/verify/{ph}")
        assert r.json()["settled"] is False
    assert calls["settled"] == settled_before + 5


def test_p6_pathological_ppm_raises_instead_of_hanging(monkeypatch):
    # post-construction mutation bypasses pydantic (tests do exactly this) -
    # the iteration cap inside _min_sendable_msat is the second line of
    # defense: a config that used to spin a worker at 100% CPU forever now
    # raises, quickly and loudly
    monkeypatch.setattr(settings, "base_fee_msat", 0)
    monkeypatch.setattr(settings, "fee_percent_ppm", 1_000_000)
    monkeypatch.setattr(settings, "min_mint_msat", 10_000)
    monkeypatch.setattr(settings, "min_sendable_msat", 10_000)

    result = {}

    def run():
        try:
            result["value"] = _min_sendable_msat()
        except RuntimeError as exc:
            result["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=30.0)
    assert not t.is_alive(), "_min_sendable_msat hung despite the iteration cap"
    assert "error" in result

    # and a high-but-legal ppm (at the validated bound) still terminates
    monkeypatch.setattr(settings, "fee_percent_ppm", 100_000)
    assert _min_sendable_msat() > 0
