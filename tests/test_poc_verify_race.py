"""Regression tests for the /verify preimage observer race (2026-08-17
review, F-3/P1 - originally PoC B3).

The race itself is spec-shaped and remains BY DESIGN when verify is on:
/verify hands a settled mint's preimage (= the bearer note's spend secret)
to ANYONE who knows the payment_hash (embedded in the invoice itself), and
the first rotater wins the note. A spec-compliant wallet rotates the moment
its payment settles and wins by construction; slow (manual/custodial)
claimants lose to any observer of the invoice. These tests pin that
behavior honestly rather than pretend it away.

What the review changed: VERIFY_ENABLED=false is now a real off switch -
the endpoint 404s entirely (not just its advertisement), closing the hole
for operators unwilling to serve spend secrets to any invoice holder. The
final test pins that.
"""

from hashlib import sha256

import bolt11
from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from lnurl_mint.db import notes
from tests.conftest import FakeNode, fake_invoice, fresh_secret


def test_full_theft_chain_attacker_rotates_first(client: TestClient, node: FakeNode, monkeypatch):
    # the race requires the endpoint to be served at all - verify ON
    monkeypatch.setattr(settings, "verify_enabled", True)

    # victim requests a mint invoice for 50_000 msat and pays it
    resp = client.get("/p/cb?amount=50000")
    victim_pr = resp.json()["pr"]
    victim_ph = bolt11.decode(victim_pr).payment_hash  # what the attacker knows
    node.settled.add(victim_ph)  # the Lightning payment itself

    # ATTACKER (knowing only payment_hash): poll verify until settled.
    # No auth, no rate limit.
    r = client.get(f"/verify/{victim_ph}")
    body = r.json()
    assert body["settled"] is True
    stolen_preimage = body["preimage"]
    assert stolen_preimage is not None
    # the preimage really is the note's spend secret: sha256(preimage) ==
    # the note id == the payment hash
    assert sha256(bytes.fromhex(stolen_preimage)).hexdigest() == victim_ph
    assert notes.note_amount(victim_ph) == 50_000

    # attacker rotates the note onto their own secret - the mint asks no
    # questions beyond a valid k1
    attacker_secret, attacker_h = fresh_secret()
    r = client.get(f"/w/cb?k1={stolen_preimage}&h={attacker_h}")
    assert r.json()["status"] == "OK", r.text
    assert notes.note_amount(attacker_h) == 50_000  # attacker now owns the value

    # VICTIM finally does what LUD-25 tells them to (rotate immediately) -
    # too late: their preimage is already spent
    _, victim_h = fresh_secret()
    r = client.get(f"/w/cb?k1={stolen_preimage}&h={victim_h}")
    assert r.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    r = client.get(f"/w?k1={stolen_preimage}")
    assert r.json() == {"status": "ERROR", "reason": "Note already spent."}

    # attacker cashes the stolen note out over Lightning
    r = client.get(f"/w/cb?k1={attacker_secret}&pr={fake_invoice(50_000)}")
    assert r.json()["status"] == "OK", r.text
    assert notes.note_amount(attacker_h) is None  # gone - theft complete


def test_race_is_purely_first_come_victim_wins_if_faster(client: TestClient, node: FakeNode, monkeypatch):
    """Same setup, but the victim rotates first (what a spec-compliant
    wallet does the moment its payment settles): the attacker's identical
    rotate then fails. Ownership is purely first-come - whoever presents
    the preimage to /w/cb first owns the note."""
    monkeypatch.setattr(settings, "verify_enabled", True)
    resp = client.get("/p/cb?amount=50000")
    victim_ph = bolt11.decode(resp.json()["pr"]).payment_hash
    node.settled.add(victim_ph)

    # victim rotates immediately (per LUD-25's Security considerations)
    preimage = node.last_preimage.hex()
    victim_secret, victim_h = fresh_secret()
    r = client.get(f"/w/cb?k1={preimage}&h={victim_h}")
    assert r.json()["status"] == "OK", r.text
    assert notes.note_amount(victim_h) == 50_000

    # attacker, having scraped the preimage from /verify, tries the same
    r = client.get(f"/verify/{victim_ph}")
    assert r.json()["preimage"] == preimage
    _, attacker_h = fresh_secret()
    r = client.get(f"/w/cb?k1={preimage}&h={attacker_h}")
    assert r.json() == {"status": "ERROR", "reason": "Invalid or already spent k1."}
    assert notes.note_amount(attacker_h) is None
    # the victim's note is untouched
    assert notes.note_amount(victim_h) == 50_000


def test_verify_works_before_and_after_settlement_shapes(client: TestClient, node: FakeNode, monkeypatch):
    """The exposure window in one picture: from the moment /p/cb answers,
    /verify/{ph} is live for ANY holder of the payment_hash - settled=false
    (no preimage yet) while unpaid, settled=true WITH the spend secret the
    instant the payment lands."""
    monkeypatch.setattr(settings, "verify_enabled", True)
    resp = client.get("/p/cb?amount=50000")
    ph = bolt11.decode(resp.json()["pr"]).payment_hash
    r = client.get(f"/verify/{ph}")
    assert r.json()["settled"] is False
    assert "preimage" not in r.json()  # nothing to steal yet
    node.settled.add(ph)
    r = client.get(f"/verify/{ph}")
    assert r.json()["settled"] is True
    assert r.json()["preimage"] is not None  # full spend secret, no auth


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
