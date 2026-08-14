import sqlite3
from hashlib import sha256

import bolt11
from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from lnurl_mint.db import NoteStore
from tests.conftest import fake_invoice, fresh_secret


def test_verify_url_absent_by_default(client: TestClient):
    data = client.get("/p/cb?amount=5000").json()
    assert "verify" not in data


def test_verify_url_advertised_when_enabled(client: TestClient, node, monkeypatch):
    monkeypatch.setattr(settings, "verify_enabled", True)
    data = client.get("/p/cb?amount=5000").json()
    payment_hash = sha256(node.last_preimage).hexdigest()
    assert data["verify"] == f"http://testserver/verify/{payment_hash}"


def test_verify_reports_unsettled_before_payment(client: TestClient, node):
    data = client.get("/p/cb?amount=5000").json()
    payment_hash = sha256(node.last_preimage).hexdigest()

    result = client.get(f"/verify/{payment_hash}").json()
    assert result == {"status": "OK", "settled": False, "pr": data["pr"]}
    assert "preimage" not in result


def test_verify_reports_settled_after_payment(client: TestClient, node):
    data = client.get("/p/cb?amount=5000").json()
    payment_hash = sha256(node.last_preimage).hexdigest()
    node.settled.add(payment_hash)

    result = client.get(f"/verify/{payment_hash}").json()
    assert result == {"status": "OK", "settled": True, "pr": data["pr"], "preimage": node.last_preimage.hex()}


def test_verify_withholds_the_preimage_before_settlement(client: TestClient, node):
    # the payment preimage IS the bearer note's spend secret - it's only
    # handed over once settled, so an unsettled invoice's verify response
    # must not leak it even though the node already knows it
    client.get("/p/cb?amount=5000")
    payment_hash = sha256(node.last_preimage).hexdigest()

    body = client.get(f"/verify/{payment_hash}").text
    assert node.last_preimage.hex() not in body
    assert "preimage" not in body


def test_verify_unknown_payment_hash_is_not_found(client: TestClient):
    bogus = "00" * 32
    result = client.get(f"/verify/{bogus}").json()
    assert result == {"status": "ERROR", "reason": "Not found"}


def test_verify_stays_settled_after_the_note_is_spent(client: TestClient, mint_note):
    # LUD-21 verify answers "was this invoice ever paid", not "is there a
    # spendable note right now" - those diverge once the note is rotated,
    # but the preimage - fetched live from the node, never cached - is
    # still handed back regardless, since the node retains it indefinitely
    k1 = mint_note(5000)
    payment_hash = sha256(bytes.fromhex(k1)).hexdigest()
    result = client.get(f"/verify/{payment_hash}").json()
    assert result["settled"] is True
    assert result["preimage"] == k1

    _, h = fresh_secret()
    rotated = client.get(f"/w/cb?k1={k1}&h={h}").json()
    assert rotated["status"] == "OK"

    result = client.get(f"/verify/{payment_hash}").json()
    assert result["settled"] is True
    assert result["preimage"] == k1


def test_verify_works_even_when_not_advertised(client: TestClient, node):
    # VERIFY_ENABLED only controls whether /p/cb *advertises* the URL, not
    # whether the endpoint itself responds when hit directly
    assert settings.verify_enabled is False
    data = client.get("/p/cb?amount=5000").json()
    assert "verify" not in data
    payment_hash = sha256(node.last_preimage).hexdigest()
    assert client.get(f"/verify/{payment_hash}").json()["settled"] is False


def test_melt_response_carries_no_verify_by_default(client: TestClient, node, mint_note):
    # verify_enabled is False in the test env (see conftest) - a melt's
    # response must stay a bare {"status": "OK"}, same as before LUD-25's
    # melt verify existed
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json() == {"status": "OK"}


def test_melt_response_carries_pr_and_verify_url_when_enabled(client: TestClient, node, mint_note, monkeypatch):
    monkeypatch.setattr(settings, "verify_enabled", True)
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    payment_hash = bolt11.decode(pr).payment_hash
    data = client.get(f"/w/cb?k1={k1}&pr={pr}").json()
    assert data["pr"] == pr
    assert data["verify"] == f"http://testserver/verify/{payment_hash}"


def test_melt_verify_reports_settled_and_a_matching_preimage_once_paid(
    client: TestClient, node, mint_note, monkeypatch
):
    # the preimage handed back here is proof of the *outgoing* payment's own
    # settlement, not a bearer secret - the note(s) that funded the melt are
    # already burned by the time anyone could use it
    monkeypatch.setattr(settings, "verify_enabled", True)
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    node.payment_actually_completed = True  # the node's own view: this payment settled
    data = client.get(f"/w/cb?k1={k1}&pr={pr}").json()

    payment_hash = bolt11.decode(pr).payment_hash
    result = client.get(data["verify"]).json()
    assert result["status"] == "OK"
    assert result["settled"] is True
    assert result["pr"] == pr
    assert result["preimage"] == node.melt_preimages[payment_hash].hex()


def test_melt_verify_reports_unsettled_before_the_payment_completes(client: TestClient, node, mint_note, monkeypatch):
    monkeypatch.setattr(settings, "verify_enabled", True)
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    # node.payment_actually_completed left False - is_payment_complete
    # reports "not yet", never trusted as a bearer secret before settlement
    data = client.get(f"/w/cb?k1={k1}&pr={pr}").json()

    result = client.get(data["verify"]).json()
    assert result == {"status": "OK", "settled": False, "pr": pr}
    assert "preimage" not in result


def test_melt_verify_works_even_when_not_advertised(client: TestClient, node, mint_note):
    # same convention as the mint side's verify_invoice: VERIFY_ENABLED only
    # gates whether the callback response advertises the URL, not whether
    # hitting it directly works
    assert settings.verify_enabled is False
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    node.payment_actually_completed = True
    data = client.get(f"/w/cb?k1={k1}&pr={pr}").json()
    assert "verify" not in data

    payment_hash = bolt11.decode(pr).payment_hash
    assert client.get(f"/verify/{payment_hash}").json()["settled"] is True


def test_mints_table_migrates_from_before_lud21(tmp_path):
    # a database from before this feature has a `mints` table with no `pr`
    # column - this mint has no migration framework, and telling an
    # operator to just delete their database isn't acceptable when it
    # holds real outstanding notes, so the column must be added by hand
    db_path = str(tmp_path / "pre-lud21.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE mints (payment_hash TEXT PRIMARY KEY, amount_msat INTEGER NOT NULL,"
        " minted INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO mints (payment_hash, amount_msat) VALUES ('deadbeef', 5000)")
    conn.commit()
    conn.close()

    store = NoteStore(db_path)
    assert store.pending_mint("deadbeef") == 5000
    assert store.mint_pr("deadbeef") == ""
    store.create_mint("newhash", "lnbcrt1...", 3000)
    assert store.mint_pr("newhash") == "lnbcrt1..."
