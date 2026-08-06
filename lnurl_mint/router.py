import json
import re
from hashlib import sha256
from http import HTTPStatus
from urllib.parse import urlparse

import bolt11
from fastapi import APIRouter, HTTPException, Query, Request

from .config import settings
from .db import PendingNoteError, notes
from .error_handler import LnurlErrorResponseHandler
from .models import (
    LnurlPayActionResponse,
    LnurlPayResponse,
    LnurlPayVerifyResponse,
    LnurlWithdrawResponse,
    WithdrawSuccessResponse,
)
from .node import LightningBackendConfig, create_invoice, is_invoice_settled, is_payment_complete, pay_invoice
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


def _pay_response(req: Request) -> LnurlPayResponse:
    base = settings.public_base_url(str(req.base_url))
    host = urlparse(base).hostname or req.url.hostname
    metadata = json.dumps(
        [
            ["text/plain", f"Mint an lnurlcash bearer note on {host}"],
            ["text/identifier", f"{settings.username}@{host}"],
        ]
    )
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
    settles, that preimage is an outstanding bearer note worth `amount`.

    `verify` (LUD-21, only advertised if VERIFY_ENABLED) lets a wallet with
    no node of its own poll settlement status - see verify_invoice."""
    if amount < settings.min_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too low.")
    if amount > settings.max_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too high.")
    funding_source = _funding_source()
    try:
        pr, preimage = await create_invoice(amount, funding_source)
    except Exception as exc:
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Error creating invoice: {exc!s}")
    # the preimage (the future bearer secret) reaches the buyer through the
    # Lightning payment itself and is discarded here, per the spec's
    # storing-hashes-not-secrets guidance - only the payment hash and the
    # invoice itself (for LUD-21 verify) are stored
    payment_hash = sha256(preimage).hexdigest()
    notes.create_mint(payment_hash, pr, amount)
    verify = str(req.url_for("verify_invoice", payment_hash=payment_hash)) if settings.verify_enabled else None
    return LnurlPayActionResponse(pr=pr, verify=verify)


@router.get("/verify/{payment_hash}", tags=["lnurlcash"])
async def verify_invoice(payment_hash: str) -> LnurlPayVerifyResponse:
    """LUD-21: reports whether an invoice minted via /p/cb has settled -
    looked up by payment_hash, unguessable but not itself secret, same as
    any other LUD-21 verify. Works whenever hit directly, regardless of
    VERIFY_ENABLED - that setting only controls whether /p/cb *advertises*
    this URL, the same way LUD-21 implementations elsewhere in this
    ecosystem treat their own verify toggle.

    `preimage` is never populated, unlike the spec's example response: for
    lnurlcash the preimage IS the bearer note's spend secret (see LUD-XX's
    Minting a bearer note from a payRequest), so returning it here would
    let anyone who merely knows the payment_hash - not proof of payment,
    just having seen the invoice - steal the note the instant it settles,
    racing whoever actually paid for it. `status`/`settled`/`pr` otherwise
    behave exactly per LUD-21."""
    pr = notes.mint_pr(payment_hash)
    if pr is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Not found")
    return LnurlPayVerifyResponse(settled=await _mint_settled(payment_hash), pr=pr)


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
    callback = str(req.url_for("get_withdraw_callback"))
    return LnurlWithdrawResponse(
        callback=callback,
        k1=k1,
        minWithdrawable=amount_msat,
        maxWithdrawable=amount_msat,
        defaultDescription=f"lnurlcash bearer note on {req.url.hostname}",
        mintPubkey=await mint_pubkey(settings.funding_source()),
    )


@router.get("/w/cb", tags=["lnurlcash"])
async def get_withdraw_callback(
    k1: list[str] = Query(...), pr: str | None = None, amount: int | None = None
) -> WithdrawSuccessResponse:
    """The lnurlcash callback - LUD-03 melt semantics, extended:

    - single k1 + pr: melt - the note is reserved (see NoteStore.
      mark_pending) while `pr` (of exactly its value) gets paid, then
      burned for good once that payment settles. Plain {"status": "OK"}.
      `pr` MUST NOT be combined with multiple k1s or with `amount` - merge
      (or split) first. If `pr` is itself a still-outstanding invoice this
      same mint issued (see create_mint), it's settled directly instead of
      actually being paid over Lightning - functionally a rotate reached
      via payRequest/withdrawRequest instead of the dedicated rotate
      callback, without the pointless fee and failure exposure of a node
      paying itself.
    - one k1, no pr, no amount: rotate - burned and replaced by a fresh
      note of the same value.
    - many k1 + amount, no pr: split - all burned; `k1` in the response
      carries `amount`, `change` the remainder.
    - many k1, no pr, no amount: merge - all burned, one note worth their
      sum minted.

    If any k1 is invalid, the whole request fails and nothing is burned or
    minted (see NoteStore.swap's single transaction). While a k1 is
    reserved by an in-flight melt, every other callback naming it (another
    melt, a rotate, a split, a merge) fails with reason "pending" instead
    (see NoteStore.mark_pending). Every note minted here (never on melt,
    which mints nothing) is signed per LUD-XX's Offline verification, by
    the funding source node's own key - see signing.sign_note; the fields
    are simply omitted if no funding source is configured or signing fails
    for any other reason."""
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

        try:
            notes.mark_pending(note_ids)
        except PendingNoteError:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")

        # self-payment: `pr` is itself a still-outstanding invoice this same
        # mint issued via /p/cb. Paying it over Lightning would just be the
        # funding source routing a payment back to itself, so settle it
        # directly instead: burn these notes and mark that invoice's
        # payment_hash minted, atomically, with no Lightning round-trip.
        # Whoever holds *that* invoice's preimage can then redeem the note
        # it produces exactly as if it had been paid for real.
        if decoded.has_payment_hash and notes.pending_mint(decoded.payment_hash) is not None:
            if notes.settle_mint(decoded.payment_hash) is None:
                # lost the race - a real payment or a concurrent self-payment
                # settled this same invoice first
                notes.restore(note_ids)
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, "Invoice already settled.")
            notes.finalize_melt(note_ids)
            return WithdrawSuccessResponse()

        funding_source = _funding_source()
        # notes stay merely "pending" (not yet burned) for the duration of
        # the payment attempt - per the spec, SERVICE MUST NOT burn a melted
        # k1 until the outgoing payment actually settles. A failed payment
        # releases them back to outstanding. But "failed" must mean
        # *confirmed* failed: pay_invoice can raise after the funding source
        # already completed the payment (a dropped connection, a timeout on
        # our side while it was still in flight, ...), and blindly releasing
        # the reservation on any exception would let the caller retry with a
        # *different* invoice and get this same value paid out twice. So a
        # raise here only restores once the funding source itself confirms
        # the payment did NOT go through - if even that check fails, the
        # notes are finalized as burned rather than risk restoring a payment
        # that actually succeeded.
        try:
            await pay_invoice(pr, funding_source)
        except Exception as exc:
            if not decoded.has_payment_hash:
                notes.finalize_melt(note_ids)
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Error paying invoice: {exc!s}")
            try:
                completed = await is_payment_complete(decoded.payment_hash, funding_source)
            except Exception:
                notes.finalize_melt(note_ids)
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Error paying invoice: {exc!s}") from exc
            if not completed:
                notes.restore(note_ids)
                raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Error paying invoice: {exc!s}")
        notes.finalize_melt(note_ids)
        return WithdrawSuccessResponse()

    try:
        if amount is not None:
            if not 0 < amount < total_msat:
                raise HTTPException(HTTPStatus.BAD_REQUEST, f"amount must be between 0 and {total_msat} msat.")
            new_k1, change_k1 = notes.swap(note_ids, [amount, total_msat - amount])
            funding_source = settings.funding_source()
            return WithdrawSuccessResponse(
                k1=new_k1,
                change=change_k1,
                signature=await sign_note(new_k1, amount, funding_source),
                changeSignature=await sign_note(change_k1, total_msat - amount, funding_source),
            )

        (new_k1,) = notes.swap(note_ids, [total_msat])
        return WithdrawSuccessResponse(
            k1=new_k1, signature=await sign_note(new_k1, total_msat, settings.funding_source())
        )
    except PendingNoteError:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")
