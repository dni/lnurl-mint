import asyncio
import logging

from fastapi.testclient import TestClient

import lnurl_mint.errors as errors_module
import lnurl_mint.router as router_module
import lnurl_mint.server as server_module
from lnurl_mint.config import settings
from lnurl_mint.server import app
from tests.conftest import FakeNode, fake_invoice


def _leave_a_note_pending(client: TestClient, node: FakeNode, mint_note, amount_msat: int = 5000) -> str:
    """Mints a note, then melts it into a payment outcome that can't be
    confirmed either way - see router._melt_pay's left-pending fallback -
    leaving it reserved (pending=1) rather than burned or restored."""
    k1 = mint_note(amount_msat)
    node.fail_payments = True
    node.is_payment_complete_raises = True
    pr = fake_invoice(amount_msat)
    assert client.get(f"/w/cb?k1={k1}&pr={pr}").json() == {"status": "OK"}
    assert client.get(f"/w/cb?k1={k1}").json() == {"status": "ERROR", "reason": "pending"}
    return k1


def test_reconcile_finalizes_a_pending_note_once_confirmed_paid(client: TestClient, node: FakeNode, mint_note):
    k1 = _leave_a_note_pending(client, node, mint_note)

    node.is_payment_complete_raises = False
    node.payment_actually_completed = True
    asyncio.run(router_module.reconcile_pending_melts(settings.funding_source()))

    # burned for good, not just still-pending
    assert client.get(f"/w?k1={k1}").json()["status"] == "ERROR"


def test_reconcile_restores_a_pending_note_once_confirmed_not_paid(client: TestClient, node: FakeNode, mint_note):
    k1 = _leave_a_note_pending(client, node, mint_note)

    node.is_payment_complete_raises = False
    node.payment_actually_completed = False
    asyncio.run(router_module.reconcile_pending_melts(settings.funding_source()))

    assert client.get(f"/w?k1={k1}").json()["maxWithdrawable"] == 5000
    # no longer pending - a fresh melt is accepted again
    assert client.get(f"/w/cb?k1={k1}").json()["status"] == "OK"


def test_reconcile_leaves_still_unconfirmable_notes_pending_without_retrying(
    client: TestClient, node: FakeNode, mint_note
):
    # a single attempt per note, not the melt-time retry/backoff schedule -
    # otherwise a boot with several stuck notes could take minutes (see
    # _confirm_payment's delays=() from reconcile_pending_melts)
    k1 = _leave_a_note_pending(client, node, mint_note)

    node.is_payment_complete_calls = 0
    asyncio.run(router_module.reconcile_pending_melts(settings.funding_source()))

    assert node.is_payment_complete_calls == 1
    assert client.get(f"/w/cb?k1={k1}").json() == {"status": "ERROR", "reason": "pending"}


def test_reconcile_writes_still_unconfirmed_notes_to_error_log(
    client: TestClient, node: FakeNode, mint_note, monkeypatch, tmp_path
):
    # regression: this used to be a plain logging.warning (stdout only). A
    # note interrupted mid-melt by a restart never gets a chance to reach
    # _melt_pay's own log_internal_error call - every later boot's
    # reconcile attempt hits this same still-unconfirmed outcome, so
    # without this, such a note leaves no durable record anywhere, ever,
    # only ephemeral warnings that scroll out of `docker logs`.
    log_path = tmp_path / "error.log"
    test_logger = logging.getLogger("lnurl_mint.errors.test_reconcile")
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False
    test_logger.addHandler(logging.FileHandler(str(log_path), delay=True))
    monkeypatch.setattr(errors_module, "_logger", test_logger)

    k1 = _leave_a_note_pending(client, node, mint_note)
    asyncio.run(router_module.reconcile_pending_melts(settings.funding_source()))

    logged = log_path.read_text()
    assert "still unconfirmed at boot" in logged
    assert k1 not in logged  # the note's own secret must never land in a log file


def test_app_boot_reconciles_a_note_left_pending_by_a_previous_process(
    client: TestClient, node: FakeNode, mint_note, monkeypatch
):
    # end-to-end wiring check: server.py's lifespan actually calls
    # reconcile_pending_melts once the funding source is confirmed
    # reachable, not just the function in isolation. The `node` fixture
    # patches fetch_node_info for frontend.py/signing.py, but a fresh
    # TestClient's own lifespan reads server.py's own imported reference -
    # patched here too, so this second boot also sees a reachable node.
    monkeypatch.setattr(server_module, "fetch_node_info", node.fetch_node_info)
    k1 = _leave_a_note_pending(client, node, mint_note)

    node.is_payment_complete_raises = False
    node.payment_actually_completed = True
    with TestClient(app):
        pass  # boot runs and shuts down here

    assert client.get(f"/w?k1={k1}").json()["status"] == "ERROR"  # burned during that boot
