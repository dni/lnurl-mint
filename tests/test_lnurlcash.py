import json
import threading
import time
from hashlib import sha256
from os import urandom

from fastapi.testclient import TestClient

from lnurl_mint.config import settings
from tests.conftest import FakeNode, fake_invoice


def note_value(client: TestClient, k1: str) -> int | None:
    """Read a note's value the authoritative way: a (repeatable,
    non-consuming) GET on its LNURL, per the spec."""
    data = client.get(f"/w?k1={k1}").json()
    if data.get("status") == "ERROR":
        return None
    assert data["minWithdrawable"] == data["maxWithdrawable"]
    return data["maxWithdrawable"]


def test_pay_request_advertises_withdraw_link(client: TestClient):
    data = client.get("/p").json()
    assert data["tag"] == "payRequest"
    assert data["withdrawLink"] == "http://testserver/w"
    assert data["minSendable"] <= data["maxSendable"]


def test_paid_invoice_preimage_becomes_a_bearer_note(client: TestClient, node: FakeNode):
    response = client.get("/p/cb?amount=5000")
    assert response.json()["pr"]
    k1 = node.last_preimage.hex()

    # not settled yet - not a note
    assert note_value(client, k1) is None

    node.settled.add(sha256(node.last_preimage).hexdigest())
    assert note_value(client, k1) == 5000
    # the informational GET never consumes the note
    assert note_value(client, k1) == 5000


def test_pay_callback_enforces_sendable_bounds(client: TestClient):
    assert client.get("/p/cb?amount=1").json()["status"] == "ERROR"
    assert client.get("/p/cb?amount=999999999999").json()["status"] == "ERROR"


def test_pay_response_omits_mint_fee_when_free(client: TestClient):
    metadata = client.get("/p").json()["metadata"]
    assert "Mint fees:" not in metadata


def test_pay_response_advertises_mint_fee_when_configured(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "base_fee_msat", 1000)
    monkeypatch.setattr(settings, "fee_percent_ppm", 2000)
    metadata = client.get("/p").json()["metadata"]
    assert ["text/plain", "Mint fees: 1000,2000"] in json.loads(metadata)


def test_mint_credits_note_net_of_configured_fee(client: TestClient, node: FakeNode, monkeypatch):
    monkeypatch.setattr(settings, "base_fee_msat", 1000)
    monkeypatch.setattr(settings, "fee_percent_ppm", 2000)  # 0.2%

    client.get("/p/cb?amount=100000")
    node.settled.add(sha256(node.last_preimage).hexdigest())

    # 1000 flat + 0.2% of 100000 = 1000 + 200 = 1200, rounded up to the
    # nearest whole sat (2000) - see test_mint_fee_rounds_up_to_the_nearest_sat
    assert note_value(client, node.last_preimage.hex()) == 100000 - 2000


def test_mint_fee_rounds_up_to_the_nearest_sat(client: TestClient, node: FakeNode, monkeypatch):
    monkeypatch.setattr(settings, "base_fee_msat", 0)
    monkeypatch.setattr(settings, "fee_percent_ppm", 1)  # 0.0001%

    client.get("/p/cb?amount=100000000")
    node.settled.add(sha256(node.last_preimage).hexdigest())

    # 0.0001% of 100000000 = 100 msat (0.1 sat) - rounded up to a full sat
    # (1000 msat) rather than left fractional, so the mint is never short
    assert note_value(client, node.last_preimage.hex()) == 100000000 - 1000


def test_pay_callback_rejects_amount_that_cannot_cover_the_fee(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "base_fee_msat", settings.min_sendable_msat + 1)
    result = client.get(f"/p/cb?amount={settings.min_sendable_msat}").json()
    assert result == {
        "status": "ERROR",
        "reason": "Amount too low to mint a note (min 0 msat net of fees).",
    }


def test_pay_callback_rejects_amount_below_min_mint(client: TestClient, monkeypatch):
    # amount clears MIN_SENDABLE_MSAT (the gross, pre-fee bound) but still
    # can't net a note worth MIN_MINT_MSAT once fee-free amount == net
    monkeypatch.setattr(settings, "min_mint_msat", 10_000)
    result = client.get(f"/p/cb?amount={settings.min_sendable_msat}").json()
    assert result == {
        "status": "ERROR",
        "reason": "Amount too low to mint a note (min 10000 msat net of fees).",
    }


def test_mint_succeeds_at_exactly_min_mint(client: TestClient, node: FakeNode, monkeypatch):
    monkeypatch.setattr(settings, "min_mint_msat", 10_000)
    client.get("/p/cb?amount=10000")
    node.settled.add(sha256(node.last_preimage).hexdigest())
    assert note_value(client, node.last_preimage.hex()) == 10000


def test_rotate_burns_and_replaces_the_note(client: TestClient, mint_note):
    k1 = mint_note(5000)
    data = client.get(f"/w/cb?k1={k1}").json()
    assert data["status"] == "OK"
    new_k1 = data["k1"]
    assert new_k1 != k1
    assert "change" not in data
    assert note_value(client, k1) is None
    assert note_value(client, new_k1) == 5000


def test_split_mints_amount_and_change(client: TestClient, mint_note):
    k1 = mint_note(5000)
    data = client.get(f"/w/cb?k1={k1}&amount=2000").json()
    assert data["status"] == "OK"
    assert note_value(client, k1) is None
    assert note_value(client, data["k1"]) == 2000
    assert note_value(client, data["change"]) == 3000


def test_split_merges_multiple_k1s_first(client: TestClient, mint_note):
    # a split may now name several k1s at once - merge them, then split off
    # `amount`, same as merging first and splitting the result separately
    a, b = mint_note(2000), mint_note(3000)
    data = client.get(f"/w/cb?k1={a}&k1={b}&amount=1000").json()
    assert data["status"] == "OK"
    assert note_value(client, a) is None
    assert note_value(client, b) is None
    assert note_value(client, data["k1"]) == 1000
    assert note_value(client, data["change"]) == 4000


def test_split_rejects_amount_out_of_range(client: TestClient, mint_note):
    k1 = mint_note(5000)
    for amount in (0, 5000, 6000):
        assert client.get(f"/w/cb?k1={k1}&amount={amount}").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_merge_burns_all_and_mints_the_sum(client: TestClient, mint_note):
    a, b = mint_note(2000), mint_note(3000)
    data = client.get(f"/w/cb?k1={a}&k1={b}").json()
    assert data["status"] == "OK"
    assert note_value(client, a) is None
    assert note_value(client, b) is None
    assert note_value(client, data["k1"]) == 5000


def test_split_deducts_base_fee_from_change_when_mint_charges_fees(client: TestClient, mint_note, monkeypatch):
    # LUD-25: base_fee_msat comes out of change, never the requested
    # amount - minted fee-free first so the note's own value stays a clean
    # 5000, then the fee is turned on only for the split itself
    k1 = mint_note(5000)
    monkeypatch.setattr(settings, "base_fee_msat", 1000)
    data = client.get(f"/w/cb?k1={k1}&amount=2000").json()
    assert data["status"] == "OK"
    assert note_value(client, data["k1"]) == 2000
    assert note_value(client, data["change"]) == 3000 - 1000


def test_split_does_not_reapply_fee_percent_ppm(client: TestClient, mint_note, monkeypatch):
    # per LUD-25, fee_percent_ppm was already withheld once at mint time -
    # only the flat base_fee_msat is charged again on split
    k1 = mint_note(5000)
    monkeypatch.setattr(settings, "base_fee_msat", 0)
    monkeypatch.setattr(settings, "fee_percent_ppm", 500_000)  # 50%, if it were (wrongly) reapplied
    data = client.get(f"/w/cb?k1={k1}&amount=2000").json()
    assert data["status"] == "OK"
    assert note_value(client, data["change"]) == 3000


def test_split_rejects_when_change_cannot_cover_the_base_fee(client: TestClient, mint_note, monkeypatch):
    k1 = mint_note(5000)
    monkeypatch.setattr(settings, "base_fee_msat", 2000)
    # amount=4500 leaves change worth 500 before the fee - can't cover it
    result = client.get(f"/w/cb?k1={k1}&amount=4500").json()
    assert result == {"status": "ERROR", "reason": "insufficient value"}
    # rejected outright - the note is untouched, not partially burned
    assert note_value(client, k1) == 5000


def test_merge_refunds_base_fee_for_every_extra_note(client: TestClient, mint_note, monkeypatch):
    # LUD-25: merging n notes refunds (n - 1) * base_fee_msat, giving back
    # every base fee already collected beyond the single one this now-one
    # note should have cost
    a, b, c = mint_note(2000), mint_note(3000), mint_note(1000)
    monkeypatch.setattr(settings, "base_fee_msat", 500)
    data = client.get(f"/w/cb?k1={a}&k1={b}&k1={c}").json()
    assert data["status"] == "OK"
    assert note_value(client, data["k1"]) == 2000 + 3000 + 1000 + 2 * 500


def test_rotate_is_unaffected_by_mint_fees(client: TestClient, mint_note, monkeypatch):
    # rotate is a merge of one - (1 - 1) * base_fee_msat refunds nothing,
    # so a fee-charging mint still returns exactly the note's own value
    k1 = mint_note(5000)
    monkeypatch.setattr(settings, "base_fee_msat", 1000)
    data = client.get(f"/w/cb?k1={k1}").json()
    assert data["status"] == "OK"
    assert note_value(client, data["k1"]) == 5000


def test_melt_pays_invoice_of_exactly_the_notes_value(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    data = client.get(f"/w/cb?k1={k1}&pr={pr}").json()
    assert data == {"status": "OK"}  # no new note on a melt
    assert node.paid == [pr]
    assert note_value(client, k1) is None


def test_melt_rejects_multiple_k1s(client: TestClient, node: FakeNode, mint_note):
    # pr MUST NOT be combined with multiple k1s - merge first
    a, b = mint_note(2000), mint_note(3000)
    pr = fake_invoice(5000)
    assert client.get(f"/w/cb?k1={a}&k1={b}&pr={pr}").json()["status"] == "ERROR"
    assert node.paid == []
    assert note_value(client, a) == 2000
    assert note_value(client, b) == 3000


def test_melt_rejects_invoice_of_wrong_amount(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(4000)
    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json()["status"] == "ERROR"
    assert node.paid == []
    assert note_value(client, k1) == 5000


def test_failed_payment_restores_the_notes(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    node.fail_payments = True
    pr = fake_invoice(5000)
    # per LUD-03 step 6, the callback replies OK immediately and pays
    # asynchronously - a payment failure is only ever observable via the
    # note becoming spendable again, never via this response
    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json() == {"status": "OK"}
    assert note_value(client, k1) == 5000


def test_pending_note_rejects_concurrent_operations(client: TestClient, node: FakeNode, mint_note):
    # while a melt's outgoing payment is in flight, its k1 is reserved but
    # not yet burned - any other callback naming it must be rejected with
    # reason "pending", not treated as merely invalid/spent
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    node.pay_delay = 0.3
    result: dict = {}

    def melt():
        result["melt"] = client.get(f"/w/cb?k1={k1}&pr={pr}").json()

    thread = threading.Thread(target=melt)
    thread.start()
    time.sleep(0.1)  # let the melt mark the note pending before racing it
    concurrent = client.get(f"/w/cb?k1={k1}").json()
    thread.join()

    assert concurrent == {"status": "ERROR", "reason": "pending"}
    assert result["melt"]["status"] == "OK"
    assert node.paid == [pr]
    assert note_value(client, k1) is None


def test_pending_note_is_released_if_the_payment_fails(client: TestClient, node: FakeNode, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    node.pay_delay = 0.3
    node.fail_payments = True
    result: dict = {}

    def melt():
        result["melt"] = client.get(f"/w/cb?k1={k1}&pr={pr}").json()

    thread = threading.Thread(target=melt)
    thread.start()
    time.sleep(0.1)
    concurrent = client.get(f"/w/cb?k1={k1}").json()
    thread.join()

    assert concurrent == {"status": "ERROR", "reason": "pending"}
    # the callback itself already replied OK (per LUD-03, before the
    # payment was even attempted) - the failure only shows up as the note
    # being outstanding again
    assert result["melt"]["status"] == "OK"
    assert note_value(client, k1) == 5000


def test_definite_payment_failure_restores_immediately_without_the_fallback_check(
    client: TestClient, node: FakeNode, mint_note
):
    # a routing failure is the funding source's own definitive answer, not
    # an ambiguous one - melt should restore the note right away, without
    # even calling the fallback is_payment_complete check
    k1 = mint_note(5000)
    node.fail_reason = "Could not find a route to pay this invoice."
    pr = fake_invoice(5000)

    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json() == {"status": "OK"}

    assert note_value(client, k1) == 5000
    assert node.is_payment_complete_called is False


def test_pending_note_is_released_if_funding_source_becomes_unavailable(
    client: TestClient, node: FakeNode, mint_note, monkeypatch
):
    # regression: a note must not get stuck "pending" forever if something
    # goes wrong *after* it's reserved but *before* any payment is even
    # attempted - e.g. the funding source becomes unconfigured between
    # requests. Previously the reservation was never released in this case,
    # permanently stranding the note despite no payment ever being tried.
    k1 = mint_note(5000)
    pr = fake_invoice(5000)
    assert note_value(client, k1) == 5000  # materialize the note before the funding source disappears
    monkeypatch.setattr(settings, "fundingsource_backend", None)

    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json()["status"] == "ERROR"
    assert node.paid == []

    monkeypatch.setattr(settings, "fundingsource_backend", "lnd")
    # the note must still be usable, not stuck pending forever
    assert note_value(client, k1) == 5000
    assert client.get(f"/w/cb?k1={k1}").json()["status"] == "OK"


def test_melt_rejects_own_pending_invoice(client: TestClient, node: FakeNode, mint_note):
    # melting straight into an invoice this same mint issued (and hasn't
    # settled yet) is rejected outright - it must not be paid over
    # Lightning (a self-payment to our own node) nor settled as a shortcut
    k1 = mint_note(5000)
    response = client.get("/p/cb?amount=5000")
    pr = response.json()["pr"]
    new_k1 = node.last_preimage.hex()

    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json()["status"] == "ERROR"

    assert node.paid == []
    assert note_value(client, k1) == 5000  # note untouched, still spendable
    assert note_value(client, new_k1) is None  # the own invoice never got settled


def test_melt_rejects_already_settled_own_invoice(client: TestClient, node: FakeNode, mint_note):
    # same rejection applies once the invoice is already settled/minted -
    # this mint issued it either way, so it's still "an invoice we created
    # ourselves"
    k1 = mint_note(5000)
    settled_k1 = mint_note(5000)
    # mint_note only settles at the (fake) node - force this mint to
    # actually observe and record that settlement (minted=1)
    assert note_value(client, settled_k1) == 5000
    pr = fake_invoice(5000, sha256(bytes.fromhex(settled_k1)).hexdigest())

    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json()["status"] == "ERROR"

    assert node.paid == []
    assert note_value(client, k1) == 5000


def test_ambiguously_failed_payment_that_actually_succeeded_does_not_restore(
    client: TestClient, node: FakeNode, mint_note
):
    # pay_invoice raised (e.g. the response was lost), but the funding
    # source confirms the payment actually completed - the melt genuinely
    # succeeded despite the local error, so this reports OK (not an error)
    # and, crucially, does NOT restore the note: doing so would let a
    # retry with a *different* invoice pay the same value out twice
    k1 = mint_note(5000)
    node.fail_payments = True
    node.payment_actually_completed = True
    pr = fake_invoice(5000)
    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json() == {"status": "OK"}
    assert note_value(client, k1) is None


def test_undeterminable_payment_status_does_not_restore(client: TestClient, node: FakeNode, mint_note):
    # if pay_invoice fails AND the confirmation check itself can't tell
    # whether the payment went through, err toward *not* restoring - an
    # honest caller might lose this note's value in this rare case, but
    # that's preferable to risking a double payout if it actually succeeded
    k1 = mint_note(5000)
    node.fail_payments = True
    node.is_payment_complete_raises = True
    pr = fake_invoice(5000)
    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json() == {"status": "OK"}
    assert note_value(client, k1) is None


def test_any_invalid_k1_fails_the_whole_request(client: TestClient, mint_note):
    k1 = mint_note(5000)
    bogus = urandom(32).hex()
    assert client.get(f"/w/cb?k1={k1}&k1={bogus}").json()["status"] == "ERROR"
    # the valid note was not burned
    assert note_value(client, k1) == 5000


def test_duplicate_k1_cannot_be_double_counted(client: TestClient, mint_note):
    k1 = mint_note(5000)
    assert client.get(f"/w/cb?k1={k1}&k1={k1}").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_too_many_k1s_is_rejected(client: TestClient):
    query = "&".join(f"k1={urandom(32).hex()}" for _ in range(settings.max_k1s + 1))
    result = client.get(f"/w/cb?{query}").json()
    assert result == {"status": "ERROR", "reason": f"Too many k1s (max {settings.max_k1s})."}


def test_amount_cannot_be_combined_with_pr(client: TestClient, mint_note):
    k1 = mint_note(5000)
    pr = fake_invoice(2000)
    assert client.get(f"/w/cb?k1={k1}&pr={pr}&amount=2000").json()["status"] == "ERROR"
    assert note_value(client, k1) == 5000


def test_withdraw_response_echoes_the_literal_secret(client: TestClient, mint_note):
    # the k1 in a withdrawRequest response MUST be the actual bearer secret,
    # never a derived or opaque identifier - a wallet relies on copying it
    # verbatim into the callback or a new note URL
    k1 = mint_note(5000)
    assert client.get(f"/w?k1={k1}").json()["k1"] == k1


def test_withdraw_requires_k1(client: TestClient):
    assert client.get("/w").json()["status"] == "ERROR"


def test_withdraw_ignores_the_declared_amount(client: TestClient, mint_note):
    # a note's URL may carry a wallet-declared &amount=, which the
    # informational endpoint MUST ignore - maxWithdrawable stays authoritative
    k1 = mint_note(5000)
    data = client.get(f"/w?k1={k1}&amount=1").json()
    assert data["maxWithdrawable"] == 5000


def test_no_bearer_secret_is_ever_persisted(client: TestClient, mint_note):
    from lnurl_mint.db import notes

    k1 = mint_note(5000)
    data = client.get(f"/w/cb?k1={k1}&amount=2000").json()
    stored = str(notes.conn.execute("SELECT * FROM notes").fetchall())
    stored += str(notes.conn.execute("SELECT * FROM mints").fetchall())
    for secret in (k1, data["k1"], data["change"]):
        assert secret not in stored
        assert sha256(bytes.fromhex(secret)).hexdigest() in stored


def test_spent_k1_cannot_be_replayed(client: TestClient, mint_note):
    k1 = mint_note(5000)
    first = client.get(f"/w/cb?k1={k1}").json()
    assert first["status"] == "OK"
    second = client.get(f"/w/cb?k1={k1}").json()
    assert second["status"] == "ERROR"
    # the replacement from the first rotate is untouched by the replay
    assert note_value(client, first["k1"]) == 5000
