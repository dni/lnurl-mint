from hashlib import sha256
from os import urandom

from fastapi.testclient import TestClient

from tests.conftest import FakeNode, fake_invoice


def note_value(client: TestClient, k1: str) -> int | None:
    """Read a note's value the authoritative way: a (repeatable,
    non-consuming) GET on its LNURL, per the spec."""
    data = client.get(f"/withdraw?k1={k1}").json()
    if data.get("status") == "ERROR":
        return None
    assert data["minWithdrawable"] == data["maxWithdrawable"]
    return data["maxWithdrawable"]


def test_pay_request_advertises_withdraw_link(client: TestClient):
    data = client.get("/pay").json()
    assert data["tag"] == "payRequest"
    assert data["withdrawLink"] == "http://testserver/withdraw"
    assert data["minSendable"] <= data["maxSendable"]


def test_paid_invoice_preimage_becomes_a_bearer_note(client: TestClient, node: FakeNode):
    response = client.get("/pay/cb?amount=5000")
    assert response.json()["pr"]
    k1 = node.last_preimage.hex()

    # not settled yet - not a note
    assert note_value(client, k1) is None

    node.settled.add(sha256(node.last_preimage).hexdigest())
    assert note_value(client, k1) == 5000
    # the informational GET never consumes the note
    assert note_value(client, k1) == 5000


def test_pay_callback_enforces_sendable_bounds(client: TestClient):
    assert client.get("/pay/cb?amount=1").json()["status"] == "ERROR"
    assert client.get("/pay/cb?amount=999999999999").json()["status"] == "ERROR"


def test_rotate_burns_and_replaces_the_note(client: TestClient, mint_note):
    k1 = mint_note(5000)
    data = client.get(f"/withdraw/cb?k1={k1}").json()
    assert data["status"] == "OK"
    new_k1 = data["k1"]
    assert new_k1 != k1
    assert "change" not in data
    assert note_value(client, k1) is None
    assert note_value(client, new_k1) == 5000


def test_split_mints_amount_and_change(client: TestClient, mint_note):
    k1 = mint_note(5000)
    data = client.get(f"/withdraw/cb?k1={k1}&amount=2000").json()
    assert data["status"] == "OK"
    assert note_value(client, k1) is None
    assert note_value(client, data["k1"]) == 2000
    assert note_value(client, data["change"]) == 3000


def test_split_rejects_amount_out_of_range(client: TestClient, mint_note):
    k1 = mint_note(5000)
    for amount in (0, 5000, 6000):
        assert client.get(f"/withdraw/cb?k1={k1}&amount={amount}").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_merge_burns_all_and_mints_the_sum(client: TestClient, mint_note):
    a, b = mint_note(2000), mint_note(3000)
    data = client.get(f"/withdraw/cb?k1={a}&k1={b}").json()
    assert data["status"] == "OK"
    assert note_value(client, a) is None
    assert note_value(client, b) is None
    assert note_value(client, data["k1"]) == 5000


def test_melt_pays_invoice_of_exactly_the_notes_value(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    data = client.get(f"/withdraw/cb?k1={k1}&pr={pr}").json()
    assert data == {"status": "OK"}  # no new note on a melt
    assert node.paid == [pr]
    assert note_value(client, k1) is None


def test_melt_rejects_multiple_k1s(client: TestClient, node: FakeNode, mint_note):
    # pr MUST NOT be combined with multiple k1s - merge first
    a, b = mint_note(2000), mint_note(3000)
    pr = fake_invoice(5000)
    assert client.get(f"/withdraw/cb?k1={a}&k1={b}&pr={pr}").json()["status"] == "ERROR"
    assert node.paid == []
    assert note_value(client, a) == 2000
    assert note_value(client, b) == 3000


def test_melt_rejects_invoice_of_wrong_amount(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(4000)
    assert client.get(f"/withdraw/cb?k1={k1}&pr={pr}").json()["status"] == "ERROR"
    assert node.paid == []
    assert note_value(client, k1) == 5000


def test_failed_payment_restores_the_notes(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    node.fail_payments = True
    pr = fake_invoice(5000)
    assert client.get(f"/withdraw/cb?k1={k1}&pr={pr}").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_any_invalid_k1_fails_the_whole_request(client: TestClient, mint_note):
    k1 = mint_note(5000)
    bogus = urandom(32).hex()
    assert client.get(f"/withdraw/cb?k1={k1}&k1={bogus}").json()["status"] == "ERROR"
    # the valid note was not burned
    assert note_value(client, k1) == 5000


def test_duplicate_k1_cannot_be_double_counted(client: TestClient, mint_note):
    k1 = mint_note(5000)
    assert client.get(f"/withdraw/cb?k1={k1}&k1={k1}").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_amount_cannot_be_combined_with_pr(client: TestClient, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(2000)
    assert client.get(f"/withdraw/cb?k1={k1}&pr={pr}&amount=2000").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_withdraw_response_echoes_the_literal_secret(client: TestClient, mint_note):
    # the k1 in a withdrawRequest response MUST be the actual bearer secret,
    # never a derived or opaque identifier - a wallet relies on copying it
    # verbatim into the callback or a new note URL
    k1 = mint_note(5000)
    assert client.get(f"/withdraw?k1={k1}").json()["k1"] == k1


def test_withdraw_requires_k1(client: TestClient):
    assert client.get("/withdraw").json()["status"] == "ERROR"


def test_withdraw_ignores_the_declared_amount(client: TestClient, mint_note):
    # a note's URL may carry a wallet-declared &amount=, which the
    # informational endpoint MUST ignore - maxWithdrawable stays authoritative
    k1 = mint_note(5000)
    data = client.get(f"/withdraw?k1={k1}&amount=1").json()
    assert data["maxWithdrawable"] == 5000


def test_no_bearer_secret_is_ever_persisted(client: TestClient, mint_note):
    from lnurl_mint.db import notes

    k1 = mint_note(5000)
    data = client.get(f"/withdraw/cb?k1={k1}&amount=2000").json()
    stored = str(notes.conn.execute("SELECT * FROM notes").fetchall())
    stored += str(notes.conn.execute("SELECT * FROM mints").fetchall())
    for secret in (k1, data["k1"], data["change"]):
        assert secret not in stored
        assert sha256(bytes.fromhex(secret)).hexdigest() in stored


def test_spent_k1_cannot_be_replayed(client: TestClient, mint_note):
    k1 = mint_note(5000)
    first = client.get(f"/withdraw/cb?k1={k1}").json()
    assert first["status"] == "OK"
    second = client.get(f"/withdraw/cb?k1={k1}").json()
    assert second["status"] == "ERROR"
    # the replacement from the first rotate is untouched by the replay
    assert note_value(client, first["k1"]) == 5000
