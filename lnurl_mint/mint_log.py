import logging
import os

from .config import settings

# a dedicated append-only accounting log, separate from error.log and the
# app's normal (stdout) logging: every mint (an invoice paid in, crediting
# a new note) and melt (an invoice paid out, burning one or more notes),
# so an operator can reconcile actual Lightning routing costs against what
# this mint collects in fees (the mint fee withheld at mint time, and
# base_fee_msat on split/merge - see router.py's _mint_fee_msat and LUD-25)
# without digging through the funding source's own payment history.
#
# Written next to the database, same as error.log - see errors.py's own
# comment for why.
_logger = logging.getLogger("lnurl_mint.mint_log")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    log_path = os.path.join(os.path.dirname(settings.database_path) or ".", "mint.log")
    # delay=True, same reasoning as error.log: an unwritable directory must
    # not crash the whole app over what's only an accounting side channel.
    _handler = logging.FileHandler(log_path, delay=True)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _logger.addHandler(_handler)


def _log(message: str) -> None:
    """Never raises - a mint or melt that already succeeded must not fail
    (or lose its accounting record silently) over mint.log being
    unwritable. Falls back to stdout (docker logs), the same lesson
    error.log's own OSError handling already learned."""
    try:
        _logger.info(message)
    except OSError as exc:
        logging.error("mint.log unwritable, entry lost: %s (%s)", message, exc)


def log_mint(payment_hash: str, gross_msat: int | None, fee_msat: int | None, net_msat: int) -> None:
    """A bearer note materialized from a settled mint invoice - see
    router._mint_settled, the only caller. gross_msat/fee_msat are None
    when the funding invoice (`pr`) can't be decoded to recover the
    pre-fee amount - net_msat (the note's own value) is always known."""
    _log(f"MINT payment_hash={payment_hash} gross_msat={gross_msat} fee_msat={fee_msat} net_msat={net_msat}")


def log_melt(note_ids: list[str], amount_msat: int, routing_fee_msat: int | None) -> None:
    """An outgoing Lightning payment that melted one or more notes - see
    router._melt_pay/reconcile_pending_melts, the only callers. note_ids
    are storage ids (sha256 of each k1), never the spendable secrets
    themselves - safe to log, same as everywhere else this mint persists
    or logs note identity. routing_fee_msat is the actual fee this mint's
    own node paid, when the backend reports one - None where it can't be
    determined (a melt confirmed via is_payment_complete rather than
    pay_invoice's own direct response, e.g. after an ambiguous outcome or
    at boot-time reconcile - see router.py)."""
    _log(f"MELT note_ids={note_ids} amount_msat={amount_msat} routing_fee_msat={routing_fee_msat}")
