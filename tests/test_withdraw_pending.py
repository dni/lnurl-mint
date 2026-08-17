"""A note reserved by an in-flight melt is visibly reserved.

`/w/cb` replies OK for a melt before the payment is even attempted, and a melt
failure is never reported back through it - per the spec it is only observable
as the note becoming spendable again. Meanwhile the note is reserved
(NoteStore.mark_pending) and every mutating callback naming it fails with
reason "pending".

So a WALLET polling after a melt read a perfectly healthy note from `/w` and
then got a bare `"pending"` error from the callback, with nothing connecting
the two. With `pending` on the withdraw response, all three outcomes are
distinguishable from `/w` alone:

    reserved  -> the note answers, with pending: true
    settled   -> "Note already spent."
    failed    -> the note answers, with no pending field

That also surfaces a state which had no holder-visible signal at all:
`_melt_pay` deliberately leaves a note reserved indefinitely when an outcome
cannot be established either way, awaiting an operator. Such a note looked
perfectly spendable and was not.
"""

import sqlite3

from fastapi.testclient import TestClient

from lnurl_mint.db import NoteStore
from tests.conftest import FakeNode, fake_invoice

# the pending-window helper lives next to the other pending tests; reused here
# rather than duplicated, since holding that window open deterministically is
# the fiddly part (see its own docstring)
from tests.test_lnurlcash import _melt_in_background


def test_withdraw_reports_an_in_flight_melt_as_pending(client: TestClient, node: FakeNode, mint_note, monkeypatch):
    k1 = mint_note(5000)
    node.pay_delay = 0.3
    thread = _melt_in_background(client, k1, fake_invoice(5000), monkeypatch)
    during = client.get(f"/w?k1={k1}").json()
    thread.join()

    assert during["pending"] is True
    # the note is still real and still worth what it says
    assert during["minWithdrawable"] == during["maxWithdrawable"] == 5000
    # ...and once the melt settles it reads as spent, not as pending
    after = client.get(f"/w?k1={k1}").json()
    assert after == {"status": "ERROR", "reason": "Note already spent."}


def test_withdraw_shows_a_released_note_as_spendable_again(client: TestClient, node: FakeNode, mint_note, monkeypatch):
    # A melt whose payment is confirmed not to have gone through releases the
    # note (NoteStore.restore). That is the "failed" outcome, and it has to be
    # distinguishable from the reserved one: same note, no pending field.
    k1 = mint_note(5000)
    node.pay_delay = 0.3
    node.fail_payments = True
    thread = _melt_in_background(client, k1, fake_invoice(5000), monkeypatch)
    during = client.get(f"/w?k1={k1}").json()
    thread.join()

    assert during["pending"] is True
    released = client.get(f"/w?k1={k1}").json()
    assert released["maxWithdrawable"] == 5000
    assert "pending" not in released, "a released note must not still look reserved"


def test_withdraw_omits_pending_for_an_unreserved_note(client: TestClient, mint_note):
    # Sent only when true, never as `false` - an unreserved note's response has
    # to stay exactly what it was before this field existed
    k1 = mint_note(5000)
    assert "pending" not in client.get(f"/w?k1={k1}").json()


def test_note_status_reads_a_database_from_before_the_pending_column(tmp_path):
    # note_status selects `pending`, so it has to work on a database created
    # before that column existed - an operator with real outstanding notes
    # cannot be told to delete it (see NoteStore._add_column_if_missing).
    # Rows predating the column were never mid-melt, so 0 is right for them.
    db_path = str(tmp_path / "pre-pending.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, amount_msat INTEGER NOT NULL, spent INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO notes (id, amount_msat) VALUES ('aa', 5000)")
    conn.execute("INSERT INTO notes (id, amount_msat, spent) VALUES ('bb', 3000, 1)")
    conn.commit()
    conn.close()

    store = NoteStore(db_path)
    assert store.note_status("aa") == (5000, False, False)
    # a burned note still answers, where note_amount would collapse it to None
    assert store.note_status("bb") == (3000, True, False)
    assert store.note_status("cc") is None
