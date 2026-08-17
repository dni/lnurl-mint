import asyncio
import json
import logging
import re
from hashlib import sha256
from http import HTTPStatus
from typing import Awaitable, Callable

import bolt11
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from .config import settings
from .db import PendingNoteError, notes
from .error_handler import LnurlErrorResponseHandler
from .errors import log_internal_error
from .mint_log import log_melt, log_mint
from .models import (
    LnurlMintAddressResponse,
    LnurlPayActionResponse,
    LnurlPayResponse,
    LnurlPayVerifyResponse,
    LnurlWithdrawResponse,
    WithdrawSuccessResponse,
)
from .node import (
    LightningBackendConfig,
    create_invoice,
    fetch_node_info,
    invoice_preimage,
    is_invoice_settled,
    is_payment_complete,
    pay_invoice,
    payment_preimage,
)
from .signing import mint_pubkey, sign_note

router = APIRouter()
router.route_class = LnurlErrorResponseHandler

# a sha256 digest, hex-encoded - every k1 this mint ever issues (a payment
# preimage or a WALLET-generated secret) is exactly this shape, and so,
# per LUD-25, is h/h2 (a hash rather than a secret, but the same 32 raw
# bytes long) - anything else can be rejected before touching the database
HEX32_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
    # decoded.amount_msat is already validated equal to total_msat by the
    # caller (get_withdraw_callback) before this background task is even
    # scheduled - an amountless invoice would already have failed there
    amount_msat = decoded.amount_msat
    assert amount_msat is not None

    try:
        result = await pay_invoice(pr, funding_source, _melt_fee_limit_msat(amount_msat))
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
        # routing fee unknown here - confirmed via is_payment_complete
        # (a status check), not pay_invoice's own response, which is the
        # only place either backend reports the fee actually paid
        log_melt(note_ids, amount_msat, None)
        return

    notes.finalize_melt(note_ids)
    log_melt(note_ids, amount_msat, result.fee_msat)


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
            # fetched before finalize_melt, which marks these spent - a
            # spent note's own value is no longer readable afterward
            amount_msat = sum(notes.note_amount(note_id) or 0 for note_id in note_ids)
            notes.finalize_melt(note_ids)
            logging.info("reconcile: melt %s confirmed paid at boot - finalized", note_ids)
            # routing fee unknown here too, same reason as _melt_pay's own
            # is_payment_complete-confirmed path
            log_melt(note_ids, amount_msat, None)
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
    net_amount_msat = notes.settle_mint(payment_hash)
    if net_amount_msat is not None:
        # None means a concurrent request already settled this same
        # invoice first (see NoteStore.settle_mint) - only the call that
        # actually performed the transition logs it, never both
        _log_mint_settled(payment_hash, net_amount_msat)
    return True


def _log_mint_settled(payment_hash: str, net_amount_msat: int) -> None:
    pr = notes.mint_pr(payment_hash)
    gross_amount_msat = None
    if pr:
        try:
            gross_amount_msat = bolt11.decode(pr).amount_msat
        except Exception:
            gross_amount_msat = None
    fee_msat = gross_amount_msat - net_amount_msat if gross_amount_msat is not None else None
    log_mint(payment_hash, gross_amount_msat, fee_msat, net_amount_msat)


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


async def _melt_settled(payment_hash: str) -> bool:
    """Whether a melt's outgoing payment (paying `payment_hash`) has
    settled, for LUD-25's melt verify. Unlike _confirm_payment, which must
    tell "confirmed not paid" apart from "can't tell yet" before a note is
    ever restored or finalized, this is a read-only status check with no
    note state to protect: still pending, a hodl HTLC held open, or a
    momentary funding-source hiccup are all reported the same as "not
    settled yet" rather than raised - a wrong answer here never burns or
    restores anything, it only tells a third party to check back later."""
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return False
    try:
        return await is_payment_complete(payment_hash, funding_source)
    except Exception:
        return False


async def _melt_preimage(payment_hash: str) -> str | None:
    """Hex-encoded preimage of a settled outgoing payment, fetched live for
    LUD-25 melt verify - mirrors _mint_preimage, but payment_preimage
    (node.py) looks up a payment this mint *sent* rather than an invoice it
    issued. None if there's no funding source to ask, or the lookup fails
    for any reason - verify still reports `settled` correctly either way."""
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return None
    try:
        preimage = await payment_preimage(payment_hash, funding_source)
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
    if not HEX32_PATTERN.match(k1):
        return None
    note_id = _note_id(k1)
    amount_msat = await _note_amount_by_id(note_id)
    return (note_id, amount_msat) if amount_msat is not None else None


def _mint_fee_msat(amount_msat: int) -> int:
    """The fee withheld from a mint of `amount_msat` (LUD-25's optional mint
    fee): a flat base_fee_msat plus fee_percent_ppm parts-per-million of the
    amount paid, rounded *up* to the nearest whole sat - Lightning fees are
    conventionally sat-denominated, and rounding up (rather than leaving a
    fractional-sat msat remainder) means the mint is never short a sat,
    matching or slightly exceeding the estimate a wallet derives from the
    advertised `Mint fees: ` metadata entry (see get_lnaddress)."""
    fee_msat = settings.base_fee_msat + (amount_msat * settings.fee_percent_ppm) // 1_000_000
    return -(-fee_msat // 1000) * 1000


def _min_sendable_msat() -> int:
    """The fee-inclusive floor to actually advertise as `minSendable` - a
    wallet paying settings.min_sendable_msat gross gets net_amount_msat =
    amount - _mint_fee_msat(amount) credited (see get_pay_callback), which
    /p/cb then also holds to settings.min_mint_msat. Advertising the raw
    settings.min_sendable_msat when it doesn't clear that net floor means
    paying the advertised minimum always bounces, so this walks amount up
    from the higher of the two configured floors until its net clears
    min_mint_msat too. Bounded defensively: fee_percent_ppm is validated
    to stay well below 100% (see config.py), which guarantees the walk
    terminates quickly - the cap turns any future regression of that
    guarantee into a loud error at request time rather than a worker
    spinning at 100% CPU for the process's lifetime."""
    amount_msat = max(settings.min_sendable_msat, settings.min_mint_msat)
    for _ in range(100_000):
        if amount_msat - _mint_fee_msat(amount_msat) >= settings.min_mint_msat:
            return amount_msat
        amount_msat += 1000
    raise RuntimeError("minSendable walk did not terminate - check the fee settings (fee_percent_ppm too high?)")


def max_mintable_msat() -> int:
    """The largest a freshly minted note's own value can actually reach -
    paying the advertised maxSendable (max_sendable_msat) nets
    max_sendable_msat minus whatever _mint_fee_msat withholds at that
    amount (see get_pay_callback), so whenever a mint fee is configured the
    true ceiling on a note's value sits below the raw setting, same
    fee-aware treatment _min_sendable_msat gives the floor, just for the
    other end. No walk needed here (unlike _min_sendable_msat): the fee
    is computed directly from max_sendable_msat itself, not searched for.
    Public (unlike this module's other fee helpers) because the
    mint-address discovery response and the frontend's own mint-limits
    display both need this exact number, not just router.py."""
    return settings.max_sendable_msat - _mint_fee_msat(settings.max_sendable_msat)


def _melt_fee_limit_msat(amount_msat: int) -> int:
    """The routing-fee budget for melting a note worth `amount_msat` - per
    LUD-25, the mint fee withheld at mint time "is meant to cover whatever
    routing cost SERVICE incurs paying out this note when it is eventually
    melted", so an operator charging more gets a correspondingly higher
    tolerance for this specific melt to actually find a route, rather than
    a value unrelated to what this mint actually charges (previously: a
    flat 0.5%-or-5000msat guess for lnd, cln's xpay left to its own
    built-in max(5000msat, 1%) default). Never less than that same
    0.5%/5000msat floor, though - a fee-free or low-flat-fee mint's melts
    must not start failing to route just because their configured fee
    alone would be too tight a cap for a large note."""
    return max(round(amount_msat * 0.005), 5000, _mint_fee_msat(amount_msat))


@router.get("/.well-known/lnurlp/{username}", tags=["lnurlcash"])
def get_lnaddress(req: Request, username: str) -> LnurlPayResponse:
    """LUD-16 Lightning Address payRequest that mints lnurlcash bearer
    notes: `withdrawLink` points at the withdrawRequest endpoint
    (get_withdraw) that will recognize this mint's payment preimages -
    paying the invoice from the callback makes `<withdrawLink>?k1=<preimage>`
    a bearer note. The mint is payable at {settings.username}@{host} (see
    the frontend one-pager) - this well-known alias is this mint's only
    payRequest entry point, there's no separate bare /p."""
    if username != settings.username:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown user.")
    base, host = settings.public_base_url_and_host(str(req.base_url))
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
        minSendable=_min_sendable_msat(),
        maxSendable=settings.max_sendable_msat,
        metadata=metadata,
        withdrawLink=f"{base}/w",
    )


async def _mint_address_response(req: Request) -> LnurlMintAddressResponse:
    base, host = settings.public_base_url_and_host(str(req.base_url))
    funding_source = settings.funding_source()
    node_alias = node_uri = node_color = mint_pubkey_value = None
    node_capacity_msat = node_num_channels = node_num_peers = None
    if funding_source.backend:
        try:
            info = await fetch_node_info(funding_source)
            node_alias = info.alias
            node_uri = info.uri
            node_color = info.color
            node_capacity_msat = info.capacity_msat
            node_num_channels = info.num_channels
            node_num_peers = info.num_peers
            # same derivation as signing.mint_pubkey - reused directly
            # rather than calling that function, which would fetch_node_info
            # a second time for the exact same round trip
            mint_pubkey_value = info.uri.split("@")[0] if info.uri else None
        except Exception as exc:
            logging.warning("mint address: could not reach %s funding source: %s", funding_source.backend, exc)
    return LnurlMintAddressResponse(
        callback=f"{base}/w",
        minWithdrawable=settings.min_mint_msat,
        maxWithdrawable=max_mintable_msat(),
        defaultDescription=f"lnurlcash bearer note on {host}",
        mintPubkey=mint_pubkey_value,
        payLink=f"{base}/.well-known/lnurlp/{settings.username}",
        nodeAlias=node_alias,
        nodeUri=node_uri,
        nodeColor=node_color,
        nodeCapacityMsat=node_capacity_msat,
        nodeNumChannels=node_num_channels,
        nodeNumPeers=node_num_peers,
    )


@router.get("/.well-known/lnurlw/{username}", tags=["lnurlcash"])
async def get_mint_address(req: Request, username: str) -> LnurlMintAddressResponse:
    """Theoretical mint-address alias, the withdraw-side mirror of
    get_lnaddress above - see LnurlMintAddressResponse's own docstring for
    why this is informational only (this mint's node identity/capacity,
    the amount bounds a note can fall into, and `payLink` back to the
    payRequest side), never a functional way to withdraw this mint's own
    funds."""
    if username != settings.username:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown user.")
    return await _mint_address_response(req)


@router.get("/p/cb", tags=["lnurlcash"])
async def get_pay_callback(req: Request, amount: int) -> LnurlPayActionResponse:
    """LUD-06 callback: returns an invoice for `amount` msat whose preimage
    this mint generated itself (see node.create_invoice) - once the invoice
    settles, that preimage is an outstanding bearer note worth `amount`
    minus the advertised mint fee, if any (see _mint_fee_msat).

    `verify` (LUD-21, only advertised if VERIFY_ENABLED) lets a wallet with
    no node of its own poll settlement status - see verify_invoice."""
    if settings.sunset_mint:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "This mint is sunsetting - minting is disabled.")
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
    # config.py's own public_base_url docstring) - same as get_lnaddress
    base = settings.public_base_url(str(req.base_url))
    verify = f"{base}/verify/{payment_hash}" if settings.verify_enabled else None
    return LnurlPayActionResponse(pr=pr, verify=verify)


async def _verify_response(
    payment_hash: str,
    pr: str,
    is_settled: Callable[[str], Awaitable[bool]],
    preimage_of: Callable[[str], Awaitable[str | None]],
) -> LnurlPayVerifyResponse:
    """The shared shape of a LUD-21 verify response: settled first, then
    (only if settled) its preimage - `is_settled`/`preimage_of` are
    _mint_settled/_mint_preimage or _melt_settled/_melt_preimage, whichever
    direction `pr` came from (see verify_invoice, the only caller)."""
    settled = await is_settled(payment_hash)
    preimage = await preimage_of(payment_hash) if settled else None
    return LnurlPayVerifyResponse(settled=settled, preimage=preimage, pr=pr)


@router.get("/verify/{payment_hash}", tags=["lnurlcash"])
async def verify_invoice(payment_hash: str) -> LnurlPayVerifyResponse:
    """LUD-21: reports whether an invoice this mint issued (via /p/cb) or
    paid out (a melt, via /w/cb - LUD-25) has settled - looked up by
    payment_hash, unguessable but not itself secret, same as any other
    LUD-21 verify. Served only while VERIFY_ENABLED is on: unlike the
    usual ecosystem convention (where such a flag merely gates whether
    callbacks *advertise* the URL), false here disables the endpoint
    entirely (404) - deliberately, because for a mint the response's
    `preimage` is not mere proof of payment but the bearer note's spend
    secret itself (see below), so an operator who doesn't want it served
    needs a real off switch, not just a hidden URL. mint_pr and melt_pr
    are separate tables keyed by two different invoices' payment hashes,
    so a lookup can never accidentally match the wrong direction.

    For a mint, `preimage`, once settled, IS the bearer note's spend secret
    (see LUD-25's Minting a bearer note from a payRequest) - deliberately
    returned anyway, fetched live from the funding source rather than
    cached (see _mint_preimage), because a wallet with no node of its own
    has no other way to learn it and claim the note. Per the spec's
    Security considerations, that wallet MUST then rotate the note
    immediately: `SERVICE`'s own node already is a permanent prior holder
    of this secret, and the payment hash travels inside the invoice
    itself, so ANY holder of the invoice (a bystander seeing the QR, a
    screenshot, a log line) can win the note by polling verify and
    rotating first. A spec-compliant wallet rotates the moment its
    payment settles and wins that race by construction; manual or
    custodial flows that don't are exposed for as long as they wait.

    For a melt, `preimage` is simply that outgoing payment's own settlement
    proof (see _melt_preimage) - not a bearer secret at all, since the
    note(s) that funded it are already burned by the time anyone could use
    it. Because a BOLT-11 `pr` commits to payment_hash = sha256(preimage),
    anyone holding both can independently confirm the melt without trusting
    this mint's word for it, per LUD-25's melt verify."""
    if not settings.verify_enabled:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Not found")
    pr = notes.mint_pr(payment_hash)
    if pr is not None:
        return await _verify_response(payment_hash, pr, _mint_settled, _mint_preimage)
    pr = notes.melt_pr(payment_hash)
    if pr is not None:
        return await _verify_response(payment_hash, pr, _melt_settled, _melt_preimage)
    raise HTTPException(HTTPStatus.NOT_FOUND, "Not found")


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

    `mintPubkey` (LUD-25 Offline verification) is advertised here rather
    than on the payRequest side: a wallet paying the mint invoice can
    already recover this mint's node id from the invoice's own signature,
    so a freshly minted note needs no separate field - only notes obtained
    via this endpoint's callback (rotate/split/merge, which have no
    invoice) do."""
    resolved = await _resolve_note(k1)
    if resolved is None:
        if HEX32_PATTERN.match(k1) and notes.note_spent(_note_id(k1)):
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Note already spent.")
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Unknown note.")
    note_id, amount_msat = resolved
    # a note reserved by an in-flight melt (NoteStore.mark_pending) must
    # not be advertised as withdrawable: every mutating callback rejects
    # it with reason "pending" per spec, so an informational endpoint
    # claiming min=max=full value meanwhile would be lying - exactly the
    # lie a sell-during-melt scam needs (buyer inspects /w, pays out of
    # band, the melt settles, the note is gone). Same rejection shape as
    # /w/cb's own, per the spec's distinct "pending" reason.
    if notes.note_pending(note_id):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")
    # built from settings, not req.url_for (which is Host-header-derived,
    # spoofable via a plain Host header even behind a proxy) - same as
    # get_lnaddress/get_pay_callback
    base, host = settings.public_base_url_and_host(str(req.base_url))
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
    req: Request,
    background_tasks: BackgroundTasks,
    k1: list[str] = Query(...),
    pr: str | None = None,
    amount: int | None = None,
    h: str | None = None,
    h2: str | None = None,
) -> WithdrawSuccessResponse:
    """The lnurlcash redeem callback - see 25.md's "Redeeming a bearer
    note" table for the k1/pr/amount combinations (melt/rotate/split/merge)
    this implements. `pr` MUST NOT be combined with multiple k1s or with
    `amount` (merge or split first). `h`/`h2` are preimage hashes WALLET
    generates for the replacement note(s) - required whenever `pr` is
    absent, and `h2` additionally whenever `amount` is too - this mint
    never generates one on WALLET's behalf.

    Details the spec leaves to the implementation:
    - min_mint_msat (/p/cb's dust floor for a *fresh* mint) does not apply
      to a split's outputs - only that `change` can't go negative or land
      at exactly 0.
    - A melt reserves its note(s) (NoteStore.mark_pending) and replies
      immediately per LUD-03 step 6; `_melt_pay` pays `pr` in the
      background and only then burns or restores them (see its own
      docstring). A `pr` naming an invoice this same mint issued via
      /p/cb is rejected synchronously instead of being paid back to this
      mint's own node.
    - Every note minted here (never on melt) is signed per Offline
      verification, over the hash WALLET supplied - omitted if no funding
      source is configured or signing fails (see signing.sign_note).
    - If any k1 is invalid the whole request fails atomically
      (NoteStore.swap); a k1 already reserved by another in-flight melt
      fails with reason "pending" instead (NoteStore.mark_pending).
    - split rejects outright while SUNSET_MINT is on, same as /p/cb - see
      that setting's own docstring in config.py."""
    if len(k1) > settings.max_k1s:
        raise HTTPException(HTTPStatus.BAD_REQUEST, f"Too many k1s (max {settings.max_k1s}).")

    if pr is not None and (len(k1) > 1 or amount is not None):
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, "pr cannot be combined with multiple k1s or amount - merge or split first."
        )

    # split (amount is not None, since the check above already rejects it
    # alongside pr) grows the number of outstanding notes just like a fresh
    # mint - rejected the same way while sunsetting. rotate/merge/melt are
    # all left alone: none of them increases this mint's liability, and a
    # sunsetting operator still needs holders able to consolidate/redeem.
    if settings.sunset_mint and amount is not None:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "This mint is sunsetting - splitting is disabled.")

    # checked before any note is resolved, so an invalid/missing hash never
    # burns anything
    if pr is None:
        if h is None or not HEX32_PATTERN.match(h):
            raise HTTPException(HTTPStatus.BAD_REQUEST, "missing h")
        if amount is not None and (h2 is None or not HEX32_PATTERN.match(h2)):
            raise HTTPException(HTTPStatus.BAD_REQUEST, "missing h2")

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

        # LUD-25 melt verify: recorded unconditionally (see
        # NoteStore.record_melt), same as a mint invoice's own `pr` - the
        # endpoint serves whatever was recorded while VERIFY_ENABLED is on
        # and 404s while off (see verify_invoice), the setting also gating
        # whether it's advertised below
        melt_verify_url = None
        if decoded.has_payment_hash:
            notes.record_melt(decoded.payment_hash, pr)
            if settings.verify_enabled:
                base = settings.public_base_url(str(req.base_url))
                melt_verify_url = f"{base}/verify/{decoded.payment_hash}"

        # per LUD-03 step 6, SERVICE replies {"status": "OK"} here and only
        # then attempts the payment asynchronously - _melt_pay runs as a
        # background task after this response has already gone out, so it
        # has no way left to report a failure back to the wallet (see its
        # docstring)
        background_tasks.add_task(_melt_pay, note_ids, pr, decoded, funding_source)
        if melt_verify_url is not None:
            return WithdrawSuccessResponse(pr=pr, verify=melt_verify_url)
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
            # strictly *less than* base_fee_msat above only guards against a
            # negative change_amount - change_before_fee == base_fee_msat
            # passes that check but leaves change_amount at exactly 0, a
            # bearer note for nothing: unlike a configurable dust floor,
            # "nothing" is never a valid note value regardless of settings.
            if change_amount < 1:
                raise HTTPException(HTTPStatus.BAD_REQUEST, "insufficient value")
            # h/h2 are validated present and well-formed above, whenever
            # pr is None and amount is not - both true in this branch
            assert h is not None and h2 is not None
            notes.swap(note_ids, [h, h2], [amount, change_amount])
            funding_source = settings.funding_source()
            return WithdrawSuccessResponse(
                sig=await sign_note(h, amount, funding_source),
                sig2=await sign_note(h2, change_amount, funding_source),
            )

        # rotate is a merge of one note - the refund below is exactly 0
        # then, so it's covered by this same branch without a special case.
        # For an actual merge (n > 1), refunding (n - 1) * base_fee_msat
        # gives back every base fee already collected beyond the single one
        # this now-one note should have cost, per LUD-25.
        assert h is not None  # validated above, whenever pr is None
        refund = (len(note_ids) - 1) * settings.base_fee_msat
        merged_amount = total_msat + refund
        notes.swap(note_ids, [h], [merged_amount])
        return WithdrawSuccessResponse(sig=await sign_note(h, merged_amount, settings.funding_source()))
    except PendingNoteError:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")
    except ValueError as exc:
        # a note resolved fine above but lost a race with a concurrent
        # request before it could be burned - same known-safe message
        # _resolve_note itself uses, not an internal error
        raise HTTPException(HTTPStatus.BAD_REQUEST, str(exc))
