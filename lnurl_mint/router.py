import json
import re
from hashlib import sha256
from http import HTTPStatus
from urllib.parse import urlparse

import bolt11
from fastapi import APIRouter, HTTPException, Query, Request

from .config import settings
from .db import notes
from .error_handler import LnurlErrorResponseHandler
from .models import LnurlPayActionResponse, LnurlPayResponse, LnurlWithdrawResponse, WithdrawSuccessResponse
from .node import LightningBackendConfig, create_invoice, is_invoice_settled, pay_invoice

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


async def _note_amount_by_id(note_id: str) -> int | None:
    """Value of the outstanding note with id `note_id`, or None. An id
    without a note yet may be the payment hash of a settled-but-not-yet-
    recorded mint invoice - per the spec, once payment settles the preimage
    MUST be accepted as a bearer note, so check the funding source lazily
    here."""
    amount_msat = notes.note_amount(note_id)
    if amount_msat is not None:
        return amount_msat
    mint_amount_msat = notes.pending_mint(note_id)
    if mint_amount_msat is None:
        return None
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return None
    if not await is_invoice_settled(note_id, funding_source):
        return None
    notes.settle_mint(note_id)
    return mint_amount_msat


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
        callback=f"{base}/pay/cb",
        minSendable=settings.min_sendable_msat,
        maxSendable=settings.max_sendable_msat,
        metadata=metadata,
        withdrawLink=f"{base}/withdraw",
    )


@router.get("/pay", tags=["lnurlcash"])
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


@router.get("/pay/cb", tags=["lnurlcash"])
async def get_pay_callback(amount: int) -> LnurlPayActionResponse:
    """LUD-06 callback: returns an invoice for `amount` msat whose preimage
    this mint generated itself (see node.create_invoice) - once the invoice
    settles, that preimage is an outstanding bearer note worth `amount`."""
    if amount < settings.min_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too low.")
    if amount > settings.max_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too high.")
    funding_source = _funding_source()
    try:
        pr, preimage = await create_invoice(amount, funding_source)
    except Exception as exc:
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Error creating invoice: {exc!s}")
    # only the payment hash is stored - the preimage (the future bearer
    # secret) reaches the buyer through the Lightning payment itself and is
    # discarded here, per the spec's storing-hashes-not-secrets guidance
    notes.create_mint(sha256(preimage).hexdigest(), amount)
    return LnurlPayActionResponse(pr=pr)


@router.get("/withdraw", tags=["lnurlcash"])
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
    MUST be ignored here, never as a stand-in for the actual note value."""
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
    )


@router.get("/withdraw/cb", tags=["lnurlcash"])
async def get_withdraw_callback(
    k1: list[str] = Query(...), pr: str | None = None, amount: int | None = None
) -> WithdrawSuccessResponse:
    """The lnurlcash callback - LUD-03 melt semantics, extended:

    - single k1 + pr: melt - the note is burned, `pr` (of exactly its
      value) gets paid. Plain {"status": "OK"}. `pr` MUST NOT be combined
      with multiple k1s or with `amount` - merge (or split) first.
    - one k1, no pr, no amount: rotate - burned and replaced by a fresh
      note of the same value.
    - one k1 + amount: split - burned; `k1` in the response carries
      `amount`, `change` the remainder.
    - many k1, no pr: merge - all burned, one note worth their sum minted.

    If any k1 is invalid, the whole request fails and nothing is burned or
    minted (see NoteStore.swap's single transaction)."""
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
        funding_source = _funding_source()
        # burn first so a concurrent request can't double-spend while the
        # payment is in flight; a failed payment restores the notes intact
        notes.swap(note_ids, [])
        try:
            await pay_invoice(pr, funding_source)
        except Exception as exc:
            notes.restore(note_ids)
            raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f"Error paying invoice: {exc!s}")
        return WithdrawSuccessResponse()

    if amount is not None:
        if len(k1) != 1:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "amount requires a single k1 - merge first.")
        if not 0 < amount < total_msat:
            raise HTTPException(HTTPStatus.BAD_REQUEST, f"amount must be between 0 and {total_msat} msat.")
        new_k1, change_k1 = notes.swap(note_ids, [amount, total_msat - amount])
        return WithdrawSuccessResponse(k1=new_k1, change=change_k1)

    (new_k1,) = notes.swap(note_ids, [total_msat])
    return WithdrawSuccessResponse(k1=new_k1)
