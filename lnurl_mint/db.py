import sqlite3
import threading

from .config import settings
from .errors import log_internal_error


class PendingNoteError(Exception):
    """Raised when a callback tries to burn a note that's mid-melt (see
    NoteStore.mark_pending) - a distinct case from "invalid or already
    spent" per the spec, which requires SERVICE reject it with
    {"status": "ERROR", "reason": "pending"} rather than a generic error."""


class NoteStore:
    """The set of outstanding bearer notes this mint has issued, plus the
    pending mints (invoices whose preimage becomes a note once paid).

    No spendable secret is ever persisted: a note is keyed by its id -
    sha256(k1) - so a leaked database reveals how many notes are
    outstanding and for how much, but lets nobody spend them. For a
    minted note that id is exactly the payment hash of the invoice that
    funded it, which is why `mints` needs no preimage column and
    settling one is a plain insert under the same key. For a rotated,
    split or merged note (LUD-25), the id is a hash the WALLET itself
    generated and disclosed (`h`/`h2`) - this store (and this mint)
    never has the secret to begin with, on top of never persisting it.
    Burned notes are kept with spent=1 rather than deleted, so a
    replayed k1 fails as "already spent" instead of dangling.

    Every operation that burns and/or mints runs in a single transaction:
    per the spec, if any k1 in a multi-k1 request is invalid the whole
    request fails and no note may be burned or minted."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS notes ("
                " id TEXT PRIMARY KEY,"  # sha256(k1), never the secret itself
                " amount_msat INTEGER NOT NULL,"
                " spent INTEGER NOT NULL DEFAULT 0,"
                " pending INTEGER NOT NULL DEFAULT 0,"  # reserved by an in-flight melt, see mark_pending
                " pending_payment_hash TEXT)"  # that melt's invoice hash, for reconcile_pending_melts
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS mints ("
                " payment_hash TEXT PRIMARY KEY,"
                " pr TEXT NOT NULL,"  # LUD-21 verify only, never the secret
                " amount_msat INTEGER NOT NULL,"
                " minted INTEGER NOT NULL DEFAULT 0)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS melts ("
                " payment_hash TEXT PRIMARY KEY,"
                " pr TEXT NOT NULL)"  # the melted-into invoice, for LUD-25 melt verify
            )
            # add-a-column migrations for databases created before that
            # column existed - this mint has no other migration mechanism,
            # so the alternative would be telling an operator to delete
            # their database (real outstanding notes, not just disposable
            # dev state). See _add_column_if_missing.
            #
            # a database from before LUD-21 verify has no `pr` on `mints` -
            # existing rows predate it entirely, so they get an empty one;
            # they're pending mint invoices, not notes, and short-lived
            self._add_column_if_missing(self._conn, "mints", "pr", "TEXT NOT NULL DEFAULT ''")
            # a database from before the async-melt pending lock has no
            # `pending` on `notes` - existing rows predate it entirely, so
            # nothing was ever mid-melt and 0 is the correct default
            self._add_column_if_missing(self._conn, "notes", "pending", "INTEGER NOT NULL DEFAULT 0")
            # a database from before reconcile_pending_melts has no way to
            # look a stranded pending note's invoice back up - existing
            # pending rows (if any) predate this column and are simply
            # invisible to pending_melts until the next mark_pending call
            # touches them, same as any other pre-migration NULL
            self._add_column_if_missing(self._conn, "notes", "pending_payment_hash", "TEXT")
            self._conn.commit()
        return self._conn

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl_suffix: str) -> None:
        """`ALTER TABLE table ADD COLUMN column ddl_suffix`, unless `column`
        is already there - table/column always come from literals in this
        file, never external input. The only migration mechanism this mint
        has; see the callers above for why each one exists."""
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")

    def create_mint(self, payment_hash: str, pr: str, amount_msat: int) -> None:
        """Record an invoice whose preimage will become a bearer note worth
        `amount_msat` once the invoice settles (see settle_mint). Only the
        payment hash and the invoice itself (`pr`, for LUD-21 verify) are
        stored - the preimage reaches the buyer through the Lightning
        payment itself."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO mints (payment_hash, pr, amount_msat) VALUES (?, ?, ?)", (payment_hash, pr, amount_msat)
            )

    def pending_mint(self, payment_hash: str) -> int | None:
        """amount_msat of the not-yet-minted invoice `payment_hash`, if any."""
        row = self.conn.execute(
            "SELECT amount_msat FROM mints WHERE payment_hash = ? AND minted = 0", (payment_hash,)
        ).fetchone()
        return row[0] if row else None

    def mint_pr(self, payment_hash: str) -> str | None:
        """The invoice `payment_hash` was minted from (LUD-21 verify's `pr`
        field), or None if this mint never issued that payment_hash at all -
        regardless of whether it has since settled or been spent."""
        row = self.conn.execute("SELECT pr FROM mints WHERE payment_hash = ?", (payment_hash,)).fetchone()
        return row[0] if row else None

    def mint_settled(self, payment_hash: str) -> bool:
        """Whether the mint invoice `payment_hash` has ever settled - for
        LUD-21 verify, which must keep answering True even after the note
        it produced is later rotated/split/merged/melted away (see
        note_amount, which only answers "is there a spendable note *right
        now*", a different question)."""
        row = self.conn.execute("SELECT minted FROM mints WHERE payment_hash = ?", (payment_hash,)).fetchone()
        return bool(row and row[0])

    def settle_mint(self, payment_hash: str) -> int | None:
        """Turn a settled mint invoice into an outstanding note - its id is
        the payment hash itself (sha256 of the preimage the buyer holds).
        Returns its value, or None if a concurrent request already minted
        it (in which case the note already exists and note_amount finds
        it)."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE mints SET minted = 1 WHERE payment_hash = ? AND minted = 0", (payment_hash,)
            )
            if cursor.rowcount != 1:
                return None
            row = self.conn.execute("SELECT amount_msat FROM mints WHERE payment_hash = ?", (payment_hash,)).fetchone()
            self.conn.execute("INSERT INTO notes (id, amount_msat) VALUES (?, ?)", (payment_hash, row[0]))
            return row[0]

    def note_amount(self, note_id: str) -> int | None:
        """Value of the outstanding (unspent) note with id `note_id`
        (sha256 of its k1), or None."""
        row = self.conn.execute("SELECT amount_msat FROM notes WHERE id = ? AND spent = 0", (note_id,)).fetchone()
        return row[0] if row else None

    def note_spent(self, note_id: str) -> bool:
        """Whether `note_id` names a note this mint actually issued and has
        since burned - as opposed to one that never existed at all. Burned
        rows are kept (see the class docstring), so this is exactly what
        distinguishes "already spent" from "unknown" for callers that want
        to report which."""
        row = self.conn.execute("SELECT spent FROM notes WHERE id = ?", (note_id,)).fetchone()
        return bool(row and row[0])

    def note_pending(self, note_id: str) -> bool:
        """Whether `note_id` names an outstanding note currently reserved by
        an in-flight melt (see mark_pending). Distinct from note_spent: the
        note still exists and may return to circulation (restore), but right
        now no callback may touch it - and the informational withdraw
        endpoint must say so instead of advertising it as withdrawable (see
        router.get_withdraw), which is exactly the lie a sell-during-melt
        scam needs."""
        row = self.conn.execute("SELECT pending FROM notes WHERE id = ? AND spent = 0", (note_id,)).fetchone()
        return bool(row and row[0])

    def swap(self, burn_ids: list[str], mint_note_ids: list[str], mint_amounts: list[int]) -> None:
        """Atomically burn every note in `burn_ids` and mint one fresh note
        per (id, amount) in zip(mint_note_ids, mint_amounts). Per LUD-25,
        `mint_note_ids` are hashes the WALLET itself generated and
        disclosed (`h`/`h2` on the callback) - this side never generates,
        sees, or persists the underlying secret, only registers a note
        under the hash it was given. Raises ValueError - burning and
        minting nothing - if any burn id is unknown, already spent, or
        repeated (the second burn of a duplicate finds it spent by the
        first), or if any mint id collides with an existing note (a
        WALLET generating a fresh, unpredictable preimage each time
        should never hit this honestly) OR with any invoice this mint
        ever issued: a minted note's id IS its funding invoice's payment
        hash (see settle_mint), so a WALLET-chosen id planted under a
        *pending* invoice's payment hash would shadow that mint once paid
        and then block settle_mint's INSERT under the same key forever -
        bricking a paid mint for the price of a dust note. Raises
        PendingNoteError instead if any burn id is reserved by an
        in-flight melt (see mark_pending): per the spec, that's a
        distinct "pending" rejection, not a plain invalid/spent one."""
        with self._lock:
            try:
                with self.conn:
                    for note_id in burn_ids:
                        row = self.conn.execute(
                            "SELECT pending FROM notes WHERE id = ? AND spent = 0", (note_id,)
                        ).fetchone()
                        if row is None:
                            raise ValueError("Invalid or already spent k1.")
                        if row[0]:
                            raise PendingNoteError("pending")
                        self.conn.execute("UPDATE notes SET spent = 1 WHERE id = ?", (note_id,))
                    for note_id, amount_msat in zip(mint_note_ids, mint_amounts):
                        if self.conn.execute("SELECT 1 FROM mints WHERE payment_hash = ?", (note_id,)).fetchone():
                            # same known-safe message as any other invalid
                            # id - which table it collided with is nobody's
                            # business but the operator's
                            raise ValueError("Invalid or already spent k1.")
                        self.conn.execute("INSERT INTO notes (id, amount_msat) VALUES (?, ?)", (note_id, amount_msat))
            except sqlite3.Error as exc:
                # exc's own text (raw sqlite3 error) is never handed back on
                # the wire - see log_internal_error
                raise ValueError(log_internal_error("Note swap failed", exc)) from exc

    def record_melt(self, payment_hash: str, pr: str) -> None:
        """Record which invoice a melt is paying into, keyed by its
        payment_hash - for LUD-25's melt verify (router.verify_invoice),
        which needs `pr` back long after the note(s) that funded it are
        burned and their own pending-melt bookkeeping (mark_pending/
        finalize_melt) is gone. Written unconditionally, the same way a
        mint invoice's `pr` is always stored regardless of VERIFY_ENABLED -
        that setting only gates whether the callback response *advertises*
        the verify URL, not whether hitting it directly works."""
        with self._lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO melts (payment_hash, pr) VALUES (?, ?)", (payment_hash, pr))

    def melt_pr(self, payment_hash: str) -> str | None:
        """The invoice a melt paid into (LUD-25 verify's `pr` field), or
        None if this mint never recorded a melt for that payment_hash."""
        row = self.conn.execute("SELECT pr FROM melts WHERE payment_hash = ?", (payment_hash,)).fetchone()
        return row[0] if row else None

    def mark_pending(self, note_ids: list[str], payment_hash: str) -> None:
        """Reserve `note_ids` for an in-flight melt without burning them yet.
        Per the spec, SERVICE MUST NOT burn a melted k1 until its outgoing
        payment actually settles, but every other callback naming one of
        these ids meanwhile (another melt, a rotate, a split, a merge) MUST
        be rejected with reason "pending" - see swap and this method's
        raises. All-or-nothing, like swap. Follow with finalize_melt once
        the payment settles, or restore if it doesn't - or, if the process
        stops before either happens, reconcile_pending_melts picks up
        `payment_hash` (persisted alongside the reservation, since that's
        what a later confirmation check needs) at the next boot."""
        with self._lock, self.conn:
            for note_id in note_ids:
                row = self.conn.execute("SELECT pending FROM notes WHERE id = ? AND spent = 0", (note_id,)).fetchone()
                if row is None:
                    raise ValueError("Invalid or already spent k1.")
                if row[0]:
                    raise PendingNoteError("pending")
            for note_id in note_ids:
                self.conn.execute(
                    "UPDATE notes SET pending = 1, pending_payment_hash = ? WHERE id = ?", (payment_hash, note_id)
                )

    def finalize_melt(self, note_ids: list[str]) -> None:
        """Burn notes for good once their melt's outgoing payment has
        settled (or is confirmed/assumed unrecoverable - see router.py) -
        the counterpart to mark_pending that actually spends them. A melt
        mints nothing, unlike swap."""
        with self._lock, self.conn:
            for note_id in note_ids:
                self.conn.execute(
                    "UPDATE notes SET spent = 1, pending = 0, pending_payment_hash = NULL WHERE id = ?", (note_id,)
                )

    def restore(self, note_ids: list[str]) -> None:
        """Release notes reserved by mark_pending after their melt's
        outgoing payment failed (and is confirmed not to have gone through)
        - the notes were never burned, so this just clears the reservation,
        leaving them outstanding again."""
        with self._lock, self.conn:
            for note_id in note_ids:
                self.conn.execute("UPDATE notes SET pending = 0, pending_payment_hash = NULL WHERE id = ?", (note_id,))

    def pending_melts(self) -> dict[str, list[str]]:
        """Every note currently reserved by an in-flight melt (see
        mark_pending), grouped by the payment_hash their outgoing payment
        was for - every note_id burned together into one melt shares the
        same hash, the same grouping mark_pending itself received. A note
        only shows up here across a restart if its melt's outcome was never
        resolved before the process stopped (a crash, or _melt_pay's own
        left-pending fallback for a genuinely unconfirmable outcome - see
        its docstring); reconcile_pending_melts is what resolves it.
        Excludes pre-migration pending rows with no recorded hash (see the
        ALTER TABLE note above) - nothing else can look their invoice up."""
        grouped: dict[str, list[str]] = {}
        for note_id, payment_hash in self.conn.execute(
            "SELECT id, pending_payment_hash FROM notes WHERE pending = 1 AND spent = 0"
        ):
            if payment_hash is not None:
                grouped.setdefault(payment_hash, []).append(note_id)
        return grouped


notes = NoteStore(settings.database_path)
