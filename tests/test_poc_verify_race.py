"""Regression tests for the /verify preimage observer race (2026-08-17
review, F-3/P1 - originally PoC B3).

Originally: the race was spec-shaped and remained BY DESIGN whenever verify
was on, since /verify handed a settled mint's preimage (= the no-comment
fallback note's entire spend secret) to ANYONE who knew the payment_hash
(embedded in the invoice itself), letting the first rotater win the note
regardless of who paid for it.

FIXED by LUD-25 comment protection (router.get_pay_callback /
NoteStore.mint_uses_comment): SERVICE now refuses verify outright for any
mint that skipped `comment` - there the preimage is the note's whole
secret, so the endpoint that used to hand it to any invoice holder is
closed for that mint instead. A mint that DID use `comment` still gets
verify served, but its disclosed preimage is no longer the note's secret
(the WALLET-held `secret` behind `comment` is), so the same theft chain
fails there too, for a different reason. test_theft_chain_closed_by_verify_refusal
and test_theft_chain_closed_because_comment_makes_the_preimage_harmless
below pin both halves of the fix.

What the earlier review changed, still true and unaffected by the above:
VERIFY_ENABLED=false is a real off switch - the endpoint 404s entirely (not
just its advertisement) - see test_verify_disabled_closes_the_hole.
"""

from hashlib import sha256

import bolt11
from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from lnurl_mint.db import notes
from tests.conftest import FakeNode, fake_invoice, fresh_secret


def test_theft_chain_closed_by_verify_refusal(client: TestClient, node: FakeNode, monkeypatch):
    """The original theft chain, now closed: a mint that skips `comment`
    (LUD-25 comment protection) still credits k1=preimage exactly as
    before, but SERVICE now refuses to serve verify for it at all, even
    with VERIFY_ENABLED on - so the attacker's very first step (scraping
    the preimage from /verify) never gets off the ground."""
    monkeypatch.setattr(settings, "verify_enabled", True)

    # victim requests a mint invoice for 50_000 msat and pays it, WITHOUT
    # comment protection (a legacy or LNURLcash-aware-but-opted-out wallet)
    resp = client.get("/p/cb?amount=50000")
    assert "verify" not in resp.json()
    victim_pr = resp.json()["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash  # what the attacker knows
    node.settled.add(victim_ph)  # the Lightning payment itself

    # ATTACKER (knowing only payment_hash): verify is refused outright, no
    # comment was ever used for this mint
    r = client.get(f"/verify/{victim_ph}")
    assert r.json() == {"status": "ERROR", "reason": "Not found"}

    # the victim's own preimage (learned the ordinary way, from paying the
    # invoice) still redeems the note normally - only the remote-disclosure
    # endpoint is closed, not the fallback note itself. Verify's refusal
    # above never touched the lazy-settle path, so the note only actually
    # materializes here, on the rotate itself.
    preimage = node.last_preimage.hex()
    _, victim_h = fresh_secret()
    r = client.get(f"/w/cb?k1={preimage}&h={victim_h}")
    assert r.json()["status"] == "OK", r.text
    assert notes.note_amount(victim_h) == 50_000


def test_theft_chain_closed_because_comment_makes_the_preimage_harmless(
    client: TestClient, node: FakeNode, monkeypatch
):
    """The complementary fix: a WALLET that DOES use LUD-25 comment
    protection gets verify served normally, but the disclosed preimage is
    no longer the note's spend secret (the WALLET-held `secret` behind
    `comment` is) - so an attacker stealing it from /verify gets nothing to
    rotate, and the theft chain fails at its second step instead."""
    monkeypatch.setattr(settings, "verify_enabled", True)
    victim_secret, comment = fresh_secret()
    resp = client.get(f"/p/cb?amount=50000&comment={comment}")
    assert resp.json().get("verify")
    victim_pr = resp.json()["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash
    node.settled.add(victim_ph)

    # ATTACKER: verify is served (comment protection was used) and does
    # disclose the preimage...
    r = client.get(f"/verify/{victim_ph}")
    body = r.json()
    assert body["settled"] is True
    stolen_preimage = body["preimage"]
    assert stolen_preimage is not None

    # ...but it redeems nothing - it was never the note's k1 to begin with
    _, attacker_h = fresh_secret()
    r = client.get(f"/w/cb?k1={stolen_preimage}&h={attacker_h}")
    assert r.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    assert notes.note_amount(attacker_h) is None

    # only the victim's own held secret redeems the note, at their leisure -
    # no race to win, since nobody else ever had anything that worked
    _, victim_h = fresh_secret()
    r = client.get(f"/w/cb?k1={victim_secret}&h={victim_h}")
    assert r.json()["status"] == "OK", r.text
    assert notes.note_amount(victim_h) == 50_000


def test_verify_refuses_the_no_comment_fallback_before_and_after_settlement(
    client: TestClient, node: FakeNode, monkeypatch
):
    """The old exposure window in one picture, now closed at both points in
    time: from the moment /p/cb answers, /verify/{ph} 404s for ANY holder of
    the payment_hash of a no-comment mint - both while unpaid and once
    settled, never just its advertisement."""
    monkeypatch.setattr(settings, "verify_enabled", True)
    resp = client.get("/p/cb?amount=50000")
    ph = bolt11.decode(resp.json()["pr"]).payment_hash
    r = client.get(f"/verify/{ph}")
    assert r.json() == {"status": "ERROR", "reason": "Not found"}
    node.settled.add(ph)
    r = client.get(f"/verify/{ph}")
    assert r.json() == {"status": "ERROR", "reason": "Not found"}


def test_melt_direction_verify_is_harmless(client: TestClient, node: FakeNode, mint_note, monkeypatch):
    """The melt-direction analog: /verify on a melt's payment_hash returns
    the OUTGOING payment's own preimage. Harmless, as the code claims: the
    notes that funded the melt are burned by the time the preimage appears,
    and the melt preimage keys no note - rotating with it fails as
    unknown."""
    monkeypatch.setattr(settings, "verify_enabled", True)
    k1 = mint_note(50_000)
    note_id = sha256(bytes.fromhex(k1)).hexdigest()

    # victim melts their note into an external invoice
    melt_invoice = fake_invoice(50_000)
    melt_ph = bolt11.decode(melt_invoice).payment_hash
    r = client.get(f"/w/cb?k1={k1}&pr={melt_invoice}")
    assert r.json()["status"] == "OK", r.text
    # note burned (background payment succeeded in FakeNode)
    assert notes.note_amount(note_id) is None
    assert notes.note_spent(note_id) is True

    # attacker polls the melt's verify once it completes
    node.payment_actually_completed = True
    r = client.get(f"/verify/{melt_ph}")
    body = r.json()
    assert body["settled"] is True
    melt_preimage = body["preimage"]
    assert melt_preimage is not None
    assert body["pr"] == melt_invoice  # proof-of-payment bundle, per LUD-25

    # the melt preimage is NOT a bearer secret. (FakeNode.pay_invoice
    # returns a random preimage uncommitted to melt_ph; on a real node
    # sha256(preimage) == melt_ph by the BOLT-11 commitment - the
    # conclusion below is identical either way, because melt_ph keys no
    # note and no mint ever used it as a payment hash.)
    assert notes.note_amount(melt_ph) is None
    assert notes.mint_pr(melt_ph) is None
    _, attacker_h = fresh_secret()
    r = client.get(f"/w/cb?k1={melt_preimage}&h={attacker_h}")
    assert r.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    assert notes.note_amount(attacker_h) is None
    # and the original note's secret is equally dead (already burned)
    r = client.get(f"/w/cb?k1={k1}&h={attacker_h}")
    assert r.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}


def test_verify_disabled_closes_the_hole(client: TestClient, node: FakeNode):
    """The review's fix: with VERIFY_ENABLED=false (the test-env default,
    and now a REAL off switch), the endpoint 404s even for a settled mint
    whose preimage is there for the taking - an observer holding the
    payment_hash learns nothing, and the victim's slow manual rotate
    succeeds untouched."""
    assert settings.verify_enabled is False
    resp = client.get("/p/cb?amount=50000")
    victim_ph = bolt11.decode(resp.json()["pr"]).payment_hash
    node.settled.add(victim_ph)

    # the attacker polls verify exactly as in the theft chain above...
    assert client.get(f"/verify/{victim_ph}").json() == {"status": "ERROR", "reason": "Not found"}

    # ...and the victim rotates at human speed, unhurried and unrobbed
    preimage = node.last_preimage.hex()
    _, victim_h = fresh_secret()
    r = client.get(f"/w/cb?k1={preimage}&h={victim_h}")
    assert r.json()["status"] == "OK", r.text
    assert notes.note_amount(victim_h) == 50_000
