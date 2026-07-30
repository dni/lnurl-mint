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
                " spent INTEGER NOT NULL DEFAULT 0,"
                " asset TEXT)"  # NORD: genesis event id, NULL for plain cash
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS mints ("
                " payment_hash TEXT PRIMARY KEY,"
                " pr TEXT NOT NULL,"  # LUD-21 verify only, never the secret
                " amount_msat INTEGER NOT NULL,"
                " minted INTEGER NOT NULL DEFAULT 0)"
            )
            # NORD (see nostr.py): pre-committed assets waiting to be born
            # (genesis_id NULL = still queued), and the outbox of chain
            # events awaiting a relay. `tip` is the asset's current chain
            # head - the mint MUST never sign two children of one tip, and
            # persisting it here is what enforces that across restarts.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS assets ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " content TEXT NOT NULL,"
                " artwork_url TEXT,"
                " artwork_sha256 TEXT,"
                " collection TEXT,"
                " amount_msat INTEGER NOT NULL,"
                " genesis_id TEXT,"
                " tip TEXT,"
                " note_id TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                " id TEXT PRIMARY KEY,"
                " json TEXT NOT NULL,"
                " published INTEGER NOT NULL DEFAULT 0)"
            )
            # a database from before LUD-21 verify has a `mints` table
            # without `pr` - CREATE TABLE IF NOT EXISTS above is a no-op
            # against it, and this mint has no other migration mechanism,
            # so add the column by hand rather than tell an operator to
            # delete their database (which holds real outstanding notes,
            # not just disposable dev state). Existing rows predate `pr`
            # entirely, so they get an empty one - they're pending mint
            # invoices, not notes, and short-lived by nature. Same story
            # for `notes.asset`, which predates the NORD layer: existing
            # notes are plain cash, so NULL is exactly right for them.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(mints)")}
            if "pr" not in columns:
                self._conn.execute("ALTER TABLE mints ADD COLUMN pr TEXT NOT NULL DEFAULT ''")
            note_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(notes)")}
            if "asset" not in note_columns:
                self._conn.execute("ALTER TABLE notes ADD COLUMN asset TEXT")
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
        duplicate finds it spent by the first).

        An asset rides its note's lineage (NORD): when the single burned
        note carries one, the single minted note inherits it - the router
        guards that an asset note only ever reaches here as a rotate or a
        melt, and the shape check below is defense in depth, not policy."""
        new_k1s = [urandom(32).hex() for _ in mint_amounts]
        with self._lock:
            try:
                with self.conn:
                    assets: list[str] = []
                    for note_id in burn_ids:
                        row = self.conn.execute("SELECT asset FROM notes WHERE id = ?", (note_id,)).fetchone()
                        if row and row[0]:
                            assets.append(row[0])
                        cursor = self.conn.execute("UPDATE notes SET spent = 1 WHERE id = ? AND spent = 0", (note_id,))
                        if cursor.rowcount != 1:
                            raise ValueError("Invalid or already spent k1.")
                    if assets and (len(burn_ids) != 1 or len(mint_amounts) > 1):
                        raise ValueError("An asset note cannot be split or merged.")
                    carried = assets[0] if assets and mint_amounts else None
                    for k1, amount_msat in zip(new_k1s, mint_amounts):
                        note_id = sha256(bytes.fromhex(k1)).hexdigest()
                        self.conn.execute(
                            "INSERT INTO notes (id, amount_msat, asset) VALUES (?, ?, ?)",
                            (note_id, amount_msat, carried),
                        )
                        if carried:
                            self.conn.execute("UPDATE assets SET note_id = ? WHERE genesis_id = ?", (note_id, carried))
            except sqlite3.Error as exc:
                raise ValueError(f"Note swap failed: {exc!s}") from exc
        return new_k1s

    def restore(self, note_ids: list[str]) -> None:
        """Un-burn notes after a failed melt - the invoice was never paid,
        so the notes must remain outstanding."""
        with self._lock, self.conn:
            for note_id in note_ids:
                self.conn.execute("UPDATE notes SET spent = 0 WHERE id = ?", (note_id,))

    # ---- NORD assets (see nostr.py for the event side) ----

    def queue_asset(
        self,
        content: str,
        artwork_url: str | None,
        artwork_sha256: str | None,
        collection: str | None,
        amount_msat: int,
    ) -> None:
        """Pre-commit an asset: the next mint invoice settling for exactly
        `amount_msat` claims it (claim_asset) and a genesis is published.
        Queued in insertion order - a booster box, not a lottery."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO assets (content, artwork_url, artwork_sha256, collection, amount_msat)"
                " VALUES (?, ?, ?, ?, ?)",
                (content, artwork_url, artwork_sha256, collection, amount_msat),
            )

    def claim_asset(self, note_id: str, amount_msat: int) -> tuple[int, str, str | None, str | None, str | None] | None:
        """Bind the oldest still-queued asset of exactly `amount_msat` to
        the freshly settled note `note_id` - (rowid, content, artwork_url,
        artwork_sha256, collection), or None when nothing queued matches
        (the mint is then plain cash, exactly as before)."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id, content, artwork_url, artwork_sha256, collection FROM assets"
                " WHERE genesis_id IS NULL AND amount_msat = ? ORDER BY id LIMIT 1",
                (amount_msat,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE assets SET note_id = ? WHERE id = ?", (note_id, row[0]))
            return row

    def bind_genesis(self, asset_rowid: int, genesis_id: str, note_id: str) -> None:
        """Finalize a claim: the asset now IS `genesis_id` (its chain tip
        starts there) and the note carries it."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE assets SET genesis_id = ?, tip = ? WHERE id = ?", (genesis_id, genesis_id, asset_rowid)
            )
            self.conn.execute("UPDATE notes SET asset = ? WHERE id = ?", (genesis_id, note_id))

    def note_asset(self, note_id: str) -> str | None:
        """The genesis event id of the asset the note carries, or None for
        plain cash."""
        row = self.conn.execute("SELECT asset FROM notes WHERE id = ?", (note_id,)).fetchone()
        return row[0] if row and row[0] else None

    def asset_artwork(self, genesis_id: str) -> tuple[str, str] | None:
        """(url, sha256) of the asset's artwork - the withdrawRequest's
        `artwork` mirror of the genesis commitment - or None if the asset
        was queued without one."""
        row = self.conn.execute(
            "SELECT artwork_url, artwork_sha256 FROM assets WHERE genesis_id = ?", (genesis_id,)
        ).fetchone()
        return (row[0], row[1]) if row and row[0] and row[1] else None

    def asset_tip(self, genesis_id: str) -> str | None:
        """The asset's current chain head - the `prev` of its next event."""
        row = self.conn.execute("SELECT tip FROM assets WHERE genesis_id = ?", (genesis_id,)).fetchone()
        return row[0] if row and row[0] else None

    def advance_tip(self, genesis_id: str, event_id: str) -> None:
        """Move the chain head after enqueueing a transfer/melt - one
        rotate, one link, never two children of the same tip."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE assets SET tip = ? WHERE genesis_id = ?", (event_id, genesis_id))

    # ---- NORD event outbox (drained by nostr.publish_outbox_forever) ----

    def enqueue_event(self, event_id: str, event_json: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO events (id, json) VALUES (?, ?)", (event_id, event_json))

    def unpublished_events(self, limit: int = 50) -> list[tuple[str, str]]:
        """Oldest-first (id, json) still awaiting a relay's OK."""
        rows = self.conn.execute(
            "SELECT id, json FROM events WHERE published = 0 ORDER BY rowid LIMIT ?", (limit,)
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def mark_event_published(self, event_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE events SET published = 1 WHERE id = ?", (event_id,))


notes = NoteStore(settings.database_path)
