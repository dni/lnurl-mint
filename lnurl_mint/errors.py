import logging
import os
import secrets

from .config import settings

# a dedicated logger/file, kept separate from the app's normal (stdout)
# logging: internal error details (backend error bodies, connection info,
# database errors, ...) must never reach an anonymous caller directly (see
# error_handler.py, router.get_pay_callback, db.NoteStore.swap), but an
# operator still needs the full picture to debug a report - the id handed
# back to the caller is the only link between the two.
#
# Written next to the database (settings.database_path's own directory)
# rather than the process's cwd - there's no separate "data dir" setting,
# and this mint already treats that directory as where its persistent state
# lives (e.g. an operator pointing DATABASE_PATH at /var/lib/lnurl-mint/).
_logger = logging.getLogger("lnurl_mint.errors")
_logger.setLevel(logging.ERROR)
_logger.propagate = False
if not _logger.handlers:
    log_path = os.path.join(os.path.dirname(settings.database_path) or ".", "error.log")
    # delay=True: don't open (or fail to open) error.log until the first
    # error is actually logged, rather than at import time - an unwritable
    # directory must not crash the whole app over what's only a diagnostic
    # side channel, before a single request has even been served.
    _handler = logging.FileHandler(log_path, delay=True)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _logger.addHandler(_handler)


def log_internal_error(context: str, exc: BaseException) -> str:
    """Logs `exc` (with its full traceback) to error.log under a random
    reference id, and returns a client-safe message carrying only that id -
    the exception's own text is never handed back on the wire."""
    reference = secrets.token_hex(4)
    try:
        _logger.error("[%s] %s: %s", reference, context, exc, exc_info=exc)
    except OSError:
        # error.log itself is unwritable (see delay=True above) - the
        # caller's own error must still surface with its reference id, not
        # be replaced by an unhandled crash in this diagnostic-only path
        pass
    return f"{context} (reference: {reference})."
