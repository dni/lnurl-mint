import asyncio
import json
import logging
import re
from hashlib import sha256
from http import HTTPStatus
from urllib.parse import urlparse

import bolt11
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from .config import settings
from .db import PendingNoteError, notes
from .error_handler import LnurlErrorResponseHandler
from .errors import log_internal_error
from .models import (
    LnurlPayActionResponse,
    LnurlPayResponse,
    LnurlPayVerifyResponse,
    LnurlWithdrawResponse,
    WithdrawSuccessResponse,
)
from .node import (
    LightningBackendConfig,
    create_invoice,
    invoice_preimage,
    is_invoice_settled,
    is_payment_complete,
    pay_invoice,
)
from .signing import mint_pubkey, sign_note

router = APIRouter()
router.route_class = LnurlErrorResponseHandler

# every k1 this mint ever issues is 32 bytes hex (a payment preimage or
# urandom) - anything else can be rejected before touching the database
K1_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _funding_source() -> LightningBackendConfig:
    funding_source = settings.funding_source()
    if not funding_source.backend:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "This mint's funding source is not configured.")
    return funding_source


# backoff between is_payment_complete retries, when pay_invoice's own
# outcome was ambiguous - gives a momentary funding-source hiccup (the one
# case a retry can actually fix) a chance to clear before _melt_pay gives
# up and leaves the notes pending for manual reconciliation. ~31s total.
_CONFIRMATION_RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16)


async def _confirm_payment(
    payment_hash: str, funding_source: LightningBackendConfig, delays: tuple[int, ...] | None = None
) -> bool | None:
    """Retries is_payment_complete with backoff, returning its definitive
    True/False once it manages to answer, or None if every attempt raised.
    Isolated from _melt_pay's own try/except so a still-failing funding
    source doesn't get mistaken for one that answered False.

    `delays` defaults (via None, resolved here rather than bound as the
    parameter's own default - a module-level default would be captured
    once at import time, permanently, deaf to tests monkeypatching the
    module constant afterward) to the melt-time backoff, appropriate for a
    live request a wallet is waiting on. reconcile_pending_melts passes
    `()` instead - a single attempt per pending note - since retrying
    there would make a boot with several stuck notes take minutes; the
    next boot is itself the retry for whatever still can't be confirmed."""
    if delays is None:
        delays = _CONFIRMATION_RETRY_DELAYS_SECONDS
    for delay in (0, *delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await is_payment_complete(payment_hash, funding_source)
        except Exception as exc:
            logging.warning("confirm payment %s: attempt failed, retrying: %s", payment_hash, exc)
    return None


async def _melt_pay(
    note_ids: list[str], pr: str, decoded: bolt11.types.Bolt11, funding_source: LightningBackendConfig
) -> None:
    """Pays `pr` from `note_ids`, which the caller (get_withdraw_callback)
    has already validated and reserved via NoteStore.mark_pending - *after*
    that caller has already replied {"status": "OK"} to the wallet. Per
    LUD-03 step 6, SERVICE responds immediately and only then attempts the
    payment asynchronously, so this runs as a FastAPI BackgroundTask and
    has no way left to report back to the wallet: every path below must
    itself decide finalize (NoteStore.finalize_melt, burns the notes for
    good), restore (NoteStore.restore, leaves them outstanding again), or -
    when neither can be justified - leave them pending for an operator to
    resolve by hand once they've confirmed the true outcome directly
    against the funding source. Never raise. A failure only a
    wallet-visible retry could otherwise fix (e.g. paying a completely
    different invoice) has no such mechanism here - the wallet learns of a
    failed melt only by its own invoice never getting paid, per spec.

    Finalizes only once the payment is positively confirmed settled, and
    restores only once it's positively confirmed not to have gone through.
    A bearer note MUST NOT be destroyed on a guess: if the outcome can't be
    established either way even after _confirm_payment's retries, the
    notes are left pending (see log_internal_error's reference id) rather
    than assumed one way or the other - the note stays frozen and unusable
    until an operator restores or finalizes it manually, but never
    disappears on its own.

    A PaymentFailed from pay_invoice is a clean failure *response*, not
    proof no HTLC remains outstanding - a malicious payee holding a hodl
    invoice can make the funding source give up and report exactly this
    kind of failure while still holding an already-sent HTLC open (see
    PaymentFailed's own docstring). It's therefore handled the same as any
    other raise below: still confirmed independently before anything is
    restored, never treated as reason enough on its own."""
    # notes stay merely "pending" (not yet burned) for the duration of the
    # payment attempt - per the spec, SERVICE MUST NOT burn a melted k1
    # until the outgoing payment actually settles. A failed payment leaves
    # them for the caller to release back to outstanding. But "failed" must
    # mean *confirmed* failed: pay_invoice can raise (or even report a
    # clean PaymentFailed) after the funding source's own bookkeeping has
    # moved on while the underlying HTLC is still live - e.g. deliberately,
    # a hodl invoice held open by the payee - and treating that as a
    # definite failure would let the caller retry with a *different*
    # invoice while the original payment can still separately resolve,
    # paying the same note's value out twice. So this only reports failure
    # once the funding source itself confirms no HTLC remains outstanding
    # - and only finalizes once it confirms the opposite. If it can't
    # confirm either way, the notes are left pending rather than guessed
    # at in either direction.
    try:
        await pay_invoice(pr, funding_source)
    except Exception as exc:
        if not decoded.has_payment_hash:
            log_internal_error(f"melt {note_ids}: error paying invoice, nothing to confirm against - left pending", exc)
            return
        completed = await _confirm_payment(decoded.payment_hash, funding_source)
        if completed is None:
            log_internal_error(f"melt {note_ids}: could not confirm payment status after retries - left pending", exc)
            return
        if not completed:
            logging.info("melt %s: confirmed not paid (%s) - restoring", note_ids, exc)
            notes.restore(note_ids)
            return
        notes.finalize_melt(note_ids)
        return

    notes.finalize_melt(note_ids)


async def reconcile_pending_melts(funding_source: LightningBackendConfig) -> None:
    """Resolves every note _melt_pay left pending across a restart (see
    NoteStore.pending_melts) - a note only ends up there if its melt's
    outgoing payment outcome couldn't be established before the process
    stopped, whether from a crash mid-melt or _melt_pay's own left-pending
    fallback for a genuinely unconfirmable outcome (see its docstring).
    Called from server.py's lifespan at boot, once the funding source is
    known reachable, so an operator doesn't have to resolve these by hand -
    just keep the funding source up and restart. Same confirm-before-acting
    discipline as _melt_pay: a note that still can't be confirmed here is
    logged and left exactly as it was, to be retried at the next boot."""
    for payment_hash, note_ids in notes.pending_melts().items():
        completed = await _confirm_payment(payment_hash, funding_source, delays=())
        if completed is None:
            # same log_internal_error as _melt_pay's own left-pending case
            # (not just logging.warning) - otherwise a note that outlives
            # its original melt (interrupted by a restart before that
            # melt's own 31s of retries finished) never reaches error.log
            # at all: every later boot's reconcile attempt would hit this
            # same still-unconfirmed outcome and only ever warn to stdout,
            # forever, with no durable record anywhere an operator would
            # find it
            log_internal_error(
                f"reconcile: melt {note_ids} still unconfirmed at boot - left pending",
                RuntimeError(f"is_payment_complete could not confirm payment_hash={payment_hash}"),
            )
            continue
        if completed:
            notes.finalize_melt(note_ids)
            logging.info("reconcile: melt %s confirmed paid at boot - finalized", note_ids)
        else:
            notes.restore(note_ids)
            logging.info("reconcile: melt %s confirmed not paid at boot - restored", note_ids)


def _note_id(k1: str) -> str:
    """A note's storage id: sha256 over the raw k1 bytes - for a minted
    note (k1 = payment preimage) this is exactly the payment hash of the
    invoice that funded it, and it's all the store ever persists."""
    return sha256(bytes.fromhex(k1)).hexdigest()


async def _mint_settled(payment_hash: str) -> bool:
    """Whether the mint invoice `payment_hash` has ever settled - checks
    the funding source live and materializes the note (NoteStore.settle_mint)
    the first time settlement is observed. Used both to lazily resolve a
    note (see _note_amount_by_id) and by LUD-21 verify, which must keep
    reporting True forever once settled, even after the resulting note is
    later spent - unlike _note_amount_by_id, which answers a different
    question ("is there a spendable note *right now*")."""
    if notes.mint_settled(payment_hash):
        return True
    if notes.pending_mint(payment_hash) is None:
        return False
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return False
    if not await is_invoice_settled(payment_hash, funding_source):
        return False
    notes.settle_mint(payment_hash)
    return True


async def _mint_preimage(payment_hash: str) -> str | None:
    """Hex-encoded preimage of a settled mint invoice, fetched live from the
    funding source for LUD-21 verify - never cached locally (see db.py's
    store-hashes-not-secrets policy), so this hits the node on every call
    rather than being persisted anywhere. None if there's no funding source
    to ask, or the node lookup itself fails for any reason - verify still
    reports `settled` correctly either way, just without a preimage to
    hand over."""
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return None
    try:
        preimage = await invoice_preimage(payment_hash, funding_source)
    except Exception:
        return None
    return preimage.hex() if preimage else None


async def _note_amount_by_id(note_id: str) -> int | None:
    """Value of the outstanding note with id `note_id`, or None - either it
    was never minted, or it has already been spent (rotated/split/merged/
    melted away)."""
    amount_msat = notes.note_amount(note_id)
    if amount_msat is not None:
        return amount_msat
    if not await _mint_settled(note_id):
        return None
    return notes.note_amount(note_id)


async def _resolve_note(k1: str) -> tuple[str, int] | None:
    """(id, value) of the outstanding note whose bearer secret is `k1`."""
    if not K1_PATTERN.match(k1):
        return None
    note_id = _note_id(k1)
    amount_msat = await _note_amount_by_id(note_id)
    return (note_id, amount_msat) if amount_msat is not None else None


def _mint_fee_msat(amount_msat: int) -> int:
    """The fee withheld from a mint of `amount_msat` (LUD-XX's optional mint
    fee): a flat base_fee_msat plus fee_percent_ppm parts-per-million of the
    amount paid, rounded *up* to the nearest whole sat - Lightning fees are
    conventionally sat-denominated, and rounding up (rather than leaving a
    fractional-sat msat remainder) means the mint is never short a sat,
    matching or slightly exceeding the estimate a wallet derives from the
    advertised `Mint fees: ` metadata entry (see _pay_response)."""
    fee_msat = settings.base_fee_msat + (amount_msat * settings.fee_percent_ppm) // 1_000_000
    return -(-fee_msat // 1000) * 1000


def _pay_response(req: Request) -> LnurlPayResponse:
    base = settings.public_base_url(str(req.base_url))
    host = urlparse(base).hostname or req.url.hostname
    metadata_entries = [
        ["text/plain", f"Mint an lnurlcash bearer note on {host}"],
        ["text/identifier", f"{settings.username}@{host}"],
    ]
    if settings.base_fee_msat or settings.fee_percent_ppm:
        # a SERVICE that omits this entry is assumed fee-free per spec, so
        # it's only added when there's actually a fee to disclose
        metadata_entries.append(["text/plain", f"Mint fees: {settings.base_fee_msat},{settings.fee_percent_ppm}"])
    metadata = json.dumps(metadata_entries)
    return LnurlPayResponse(
        callback=f"{base}/p/cb",
        minSendable=settings.min_sendable_msat,
        maxSendable=settings.max_sendable_msat,
        metadata=metadata,
        withdrawLink=f"{base}/w",
    )


@router.get("/p", tags=["lnurlcash"])
def get_pay(req: Request) -> LnurlPayResponse:
    """LUD-06 payRequest that mints lnurlcash bearer notes: `withdrawLink`
    points at the withdrawRequest endpoint (get_withdraw) that will
    recognize this mint's payment preimages - paying the invoice from the
    callback makes `<withdrawLink>?k1=<preimage>` a bearer note."""
    return _pay_response(req)


@router.get("/.well-known/lnurlp/{username}", tags=["lnurlcash"])
def get_lnaddress(req: Request, username: str) -> LnurlPayResponse:
    """LUD-16: Lightning Address alias for the mint's payRequest - the mint
    is payable at {settings.username}@{host} (see the frontend one-pager)."""
    if username != settings.username:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown user.")
    return _pay_response(req)


@router.get("/p/cb", tags=["lnurlcash"])
async def get_pay_callback(req: Request, amount: int) -> LnurlPayActionResponse:
    """LUD-06 callback: returns an invoice for `amount` msat whose preimage
    this mint generated itself (see node.create_invoice) - once the invoice
    settles, that preimage is an outstanding bearer note worth `amount`
    minus the advertised mint fee, if any (see _mint_fee_msat).

    `verify` (LUD-21, only advertised if VERIFY_ENABLED) lets a wallet with
    no node of its own poll settlement status - see verify_invoice."""
    if amount < settings.min_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too low.")
    if amount > settings.max_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too high.")
    net_amount_msat = amount - _mint_fee_msat(amount)
    if net_amount_msat < settings.min_mint_msat:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, f"Amount too low to mint a note (min {settings.min_mint_msat} msat net of fees)."
        )
    funding_source = _funding_source()
    try:
        pr, preimage = await create_invoice(amount, funding_source)
    except Exception as exc:
        # exc's own text (backend error bodies, connection info, ...) is
        # never handed back on the wire - see log_internal_error
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, log_internal_error("Error creating invoice", exc))
    # the preimage (the future bearer secret) reaches the buyer through the
    # Lightning payment itself and is discarded here, per the spec's
    # storing-hashes-not-secrets guidance - only the payment hash and the
    # invoice itself (for LUD-21 verify) are stored. The invoice itself is
    # for the full `amount` (what the payer actually pays); the note it
    # produces is credited net of the mint fee.
    payment_hash = sha256(preimage).hexdigest()
    notes.create_mint(payment_hash, pr, net_amount_msat)
    # built from settings, not req.url_for (which is Host-header-derived,
    # spoofable via a plain Host header even behind a proxy - see
    # config.py's own public_base_url docstring) - same as _pay_response
    base = settings.public_base_url(str(req.base_url))
    verify = f"{base}/verify/{payment_hash}" if settings.verify_enabled else None
    return LnurlPayActionResponse(pr=pr, verify=verify)


@router.get("/verify/{payment_hash}", tags=["lnurlcash"])
async def verify_invoice(payment_hash: str) -> LnurlPayVerifyResponse:
    """LUD-21: reports whether an invoice minted via /p/cb has settled -
    looked up by payment_hash, unguessable but not itself secret, same as
    any other LUD-21 verify. Works whenever hit directly, regardless of
    VERIFY_ENABLED - that setting only controls whether /p/cb *advertises*
    this URL, the same way LUD-21 implementations elsewhere in this
    ecosystem treat their own verify toggle.

    `preimage`, once settled, IS the bearer note's spend secret (see
    LUD-XX's Minting a bearer note from a payRequest) - deliberately
    returned anyway, fetched live from the funding source rather than
    cached (see _mint_preimage), because a wallet with no node of its own
    has no other way to learn it and claim the note. Per the spec's
    Security considerations, that wallet MUST then rotate the note
    immediately: `SERVICE`'s own node already is a permanent prior holder
    of this secret, and verify only makes confirming settlement
    convenient, it does nothing to shrink that exposure window on its own."""
    pr = notes.mint_pr(payment_hash)
    if pr is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Not found")
    settled = await _mint_settled(payment_hash)
    preimage = await _mint_preimage(payment_hash) if settled else None
    return LnurlPayVerifyResponse(settled=settled, preimage=preimage, pr=pr)


@router.get("/w", tags=["lnurlcash"])
async def get_withdraw(req: Request, k1: str, amount: int | None = None) -> LnurlWithdrawResponse:
    """LUD-03 withdrawRequest for the bearer note `k1`. Purely informational:
    it never burns or alters the note (which is what makes it safe for any
    wallet to inspect a note's value without consuming it), so the mutating
    callback below lives on a distinct URL, as the spec requires.
    minWithdrawable == maxWithdrawable states the note's value
    authoritatively. The response's `k1` MUST echo the literal secret it was
    queried with - never a derived or opaque identifier - so a wallet can
    copy it verbatim into a new note URL or the callback.

    `amount` is accepted only because a note's URL encodes a
    (wallet-declared, unauthoritative) value as `?k1=...&amount=...` - it
    MUST be ignored here, never as a stand-in for the actual note value.

    `mintPubkey` (LUD-XX Offline verification) is advertised here rather
    than on the payRequest side: a wallet paying the mint invoice can
    already recover this mint's node id from the invoice's own signature,
    so a freshly minted note needs no separate field - only notes obtained
    via this endpoint's callback (rotate/split/merge, which have no
    invoice) do."""
    resolved = await _resolve_note(k1)
    if resolved is None:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Unknown or already spent note.")
    _, amount_msat = resolved
    # built from settings, not req.url_for (which is Host-header-derived,
    # spoofable via a plain Host header even behind a proxy) - same as
    # _pay_response/get_pay_callback
    base = settings.public_base_url(str(req.base_url))
    host = urlparse(base).hostname or req.url.hostname
    return LnurlWithdrawResponse(
        callback=f"{base}/w/cb",
        k1=k1,
        minWithdrawable=amount_msat,
        maxWithdrawable=amount_msat,
        defaultDescription=f"lnurlcash bearer note on {host}",
        mintPubkey=await mint_pubkey(settings.funding_source()),
    )


@router.get("/w/cb", tags=["lnurlcash"])
async def get_withdraw_callback(
    background_tasks: BackgroundTasks, k1: list[str] = Query(...), pr: str | None = None, amount: int | None = None
) -> WithdrawSuccessResponse:
    """The lnurlcash callback - LUD-03 melt semantics, extended:

    - single k1 + pr: melt - the note is reserved (see NoteStore.
      mark_pending) while `pr` (of exactly its value) gets paid. Per LUD-03
      step 6, this replies {"status": "OK"} as soon as the request itself
      is validated and the note reserved, then pays `pr` asynchronously in
      the background (see _melt_pay) - burned for good once that payment
      settles, released again if it definitively fails. `pr` MUST NOT be
      combined with multiple k1s or with `amount` - merge (or split)
      first. If `pr` is itself an invoice this same mint issued (see
      create_mint), pending or already settled, the melt is rejected
      outright (synchronously - no payment is ever attempted) rather than
      paying it back to the mint's own node.
    - one k1, no pr, no amount: rotate - burned and replaced by a fresh
      note of the same value.
    - many k1 + amount, no pr: split - all burned; `k1` in the response
      carries `amount`, `change` the remainder, minus base_fee_msat if
      this mint charges fees (LUD-25) - fails with reason "insufficient
      value" if that would leave change worth less than the fee.
    - many k1, no pr, no amount: merge - all burned, one note worth their
      sum minted, plus (n - 1) * base_fee_msat if this mint charges fees -
      refunding every base fee already collected beyond the single one an
      n-note merge should have cost.

    If any k1 is invalid, the whole request fails and nothing is burned or
    minted (see NoteStore.swap's single transaction). While a k1 is
    reserved by an in-flight melt, every other callback naming it (another
    melt, a rotate, a split, a merge) fails with reason "pending" instead
    (see NoteStore.mark_pending). Every note minted here (never on melt,
    which mints nothing) is signed per LUD-XX's Offline verification, by
    the funding source node's own key - see signing.sign_note; the fields
    are simply omitted if no funding source is configured or signing fails
    for any other reason."""
    if len(k1) > settings.max_k1s:
        raise HTTPException(HTTPStatus.BAD_REQUEST, f"Too many k1s (max {settings.max_k1s}).")

    if pr is not None and (len(k1) > 1 or amount is not None):
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "pr cannot be combined with multiple k1s or amount - merge or split first."
        )

    note_ids: list[str] = []
    values: list[int] = []
    for note_k1 in k1:
        resolved = await _resolve_note(note_k1)
        if resolved is None:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid or already spent k1.")
        note_ids.append(resolved[0])
        values.append(resolved[1])
    total_msat = sum(values)

    if pr is not None:
        try:
            decoded = bolt11.decode(pr)
        except Exception as exc:
            raise HTTPException(HTTPStatus.BAD_REQUEST, f"Invalid invoice: {exc!s}")
        if decoded.amount_msat != total_msat:
            raise HTTPException(HTTPStatus.BAD_REQUEST, f"Invoice must be for exactly {total_msat} msat.")
        # `pr` is itself an invoice this same mint issued via /p/cb (pending
        # or already settled/minted) - reject outright rather than paying
        # it. Paying it over Lightning would just be the funding source
        # routing a payment back to itself, which real nodes handle
        # inconsistently (some reject it as a cycle, some accept it at face
        # value); notes.mint_pr matches regardless of settlement status,
        # since this mint issued the invoice either way. Checked - and
        # rejected synchronously, unlike the actual payment below - because
        # it's a cheap local lookup, not a Lightning round-trip.
        if decoded.has_payment_hash and notes.mint_pr(decoded.payment_hash) is not None:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Cannot melt into an invoice this mint issued itself.")

        funding_source = _funding_source()

        try:
            notes.mark_pending(note_ids, decoded.payment_hash)
        except PendingNoteError:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")
        except ValueError as exc:
            # a note resolved fine above but lost a race with a concurrent
            # request before it could be reserved - same known-safe message
            # _resolve_note itself uses, not an internal error
            raise HTTPException(HTTPStatus.BAD_REQUEST, str(exc))

        # per LUD-03 step 6, SERVICE replies {"status": "OK"} here and only
        # then attempts the payment asynchronously - _melt_pay runs as a
        # background task after this response has already gone out, so it
        # has no way left to report a failure back to the wallet (see its
        # docstring)
        background_tasks.add_task(_melt_pay, note_ids, pr, decoded, funding_source)
        return WithdrawSuccessResponse()

    try:
        if amount is not None:
            if not 0 < amount < total_msat:
                raise HTTPException(HTTPStatus.BAD_REQUEST, f"amount must be between 0 and {total_msat} msat.")
            # per LUD-25, base_fee_msat (never fee_percent_ppm - that's
            # already been withheld once, at mint time) comes out of
            # change, not the requested amount, so a holder can't dodge it
            # by splitting into many dust notes and melting each
            # separately. 0 when this SERVICE is fee-free, a no-op then.
            change_before_fee = total_msat - amount
            if change_before_fee < settings.base_fee_msat:
                raise HTTPException(HTTPStatus.BAD_REQUEST, "insufficient value")
            change_amount = change_before_fee - settings.base_fee_msat
            new_k1, change_k1 = notes.swap(note_ids, [amount, change_amount])
            funding_source = settings.funding_source()
            return WithdrawSuccessResponse(
                k1=new_k1,
                change=change_k1,
                signature=await sign_note(new_k1, amount, funding_source),
                changeSignature=await sign_note(change_k1, change_amount, funding_source),
            )

        # rotate is a merge of one note - the refund below is exactly 0
        # then, so it's covered by this same branch without a special case.
        # For an actual merge (n > 1), refunding (n - 1) * base_fee_msat
        # gives back every base fee already collected beyond the single one
        # this now-one note should have cost, per LUD-25.
        refund = (len(note_ids) - 1) * settings.base_fee_msat
        merged_amount = total_msat + refund
        (new_k1,) = notes.swap(note_ids, [merged_amount])
        return WithdrawSuccessResponse(
            k1=new_k1, signature=await sign_note(new_k1, merged_amount, settings.funding_source())
        )
    except PendingNoteError:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")
    except ValueError as exc:
        # a note resolved fine above but lost a race with a concurrent
        # request before it could be burned - same known-safe message
        # _resolve_note itself uses, not an internal error
        raise HTTPException(HTTPStatus.BAD_REQUEST, str(exc))
