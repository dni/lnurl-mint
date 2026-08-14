import sqlite3
from hashlib import sha256

from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from lnurl_mint.db import NoteStore
from tests.conftest import fresh_secret


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
