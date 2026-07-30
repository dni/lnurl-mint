import sqlite3
import threading
from hashlib import sha256
from os import urandom

from .config import settings


class NoteStore:
    """The set of outstanding bearer notes this mint has issued, plus the
    pending mints (invoices whose preimage becomes a note once paid).

    Per the spec's security considerations, no spendable secret is ever
    persisted: a note is keyed by its id - sha256(k1) - so a leaked
    database reveals how many notes are outstanding and for how much, but
    lets nobody spend them. For a minted note that id is exactly the
    payment hash of the invoice that funded it, which is why `mints` needs
    no preimage column and settling one is a plain insert under the same
    key. Burned notes are kept with spent=1 rather than deleted, so a
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
                " spent INTEGER NOT NULL DEFAULT 0)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS mints ("
                " payment_hash TEXT PRIMARY KEY,"
                " pr TEXT NOT NULL,"  # LUD-21 verify only, never the secret
                " amount_msat INTEGER NOT NULL,"
                " minted INTEGER NOT NULL DEFAULT 0)"
            )
            # a database from before LUD-21 verify has a `mints` table
            # without `pr` - CREATE TABLE IF NOT EXISTS above is a no-op
            # against it, and this mint has no other migration mechanism,
            # so add the column by hand rather than tell an operator to
            # delete their database (which holds real outstanding notes,
            # not just disposable dev state). Existing rows predate `pr`
            # entirely, so they get an empty one - they're pending mint
            # invoices, not notes, and short-lived by nature.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(mints)")}
            if "pr" not in columns:
                self._conn.execute("ALTER TABLE mints ADD COLUMN pr TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        return self._conn

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

    def swap(self, burn_ids: list[str], mint_amounts: list[int]) -> list[str]:
        """Atomically burn every note in `burn_ids` and mint one fresh note
        per amount in `mint_amounts`, returning the new bearer secrets -
        the only time they ever exist on this side; only their hashes are
        stored. Raises ValueError - burning and minting nothing - if any id
        is unknown, already spent, or repeated (the second burn of a
        duplicate finds it spent by the first)."""
        new_k1s = [urandom(32).hex() for _ in mint_amounts]
        with self._lock:
            try:
                with self.conn:
                    for note_id in burn_ids:
                        cursor = self.conn.execute("UPDATE notes SET spent = 1 WHERE id = ? AND spent = 0", (note_id,))
                        if cursor.rowcount != 1:
                            raise ValueError("Invalid or already spent k1.")
                    for k1, amount_msat in zip(new_k1s, mint_amounts):
                        note_id = sha256(bytes.fromhex(k1)).hexdigest()
                        self.conn.execute("INSERT INTO notes (id, amount_msat) VALUES (?, ?)", (note_id, amount_msat))
            except sqlite3.Error as exc:
                raise ValueError(f"Note swap failed: {exc!s}") from exc
        return new_k1s

    def restore(self, note_ids: list[str]) -> None:
        """Un-burn notes after a failed melt - the invoice was never paid,
        so the notes must remain outstanding."""
        with self._lock, self.conn:
            for note_id in note_ids:
                self.conn.execute("UPDATE notes SET spent = 0 WHERE id = ?", (note_id,))


notes = NoteStore(settings.database_path)
