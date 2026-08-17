from typing import Literal

from pydantic import BaseModel


class LnurlPayResponse(BaseModel):
    """LUD-06 payRequest, extended with lnurlcash's `withdrawLink` - the raw
    (LUD-17) URL of the withdrawRequest endpoint that will recognize this
    mint's payment preimages as bearer notes."""

    tag: Literal["payRequest"] = "payRequest"
    callback: str
    minSendable: int
    maxSendable: int
    metadata: str
    withdrawLink: str


class LnurlPayActionResponse(BaseModel):
    """LUD-06 callback response - paying `pr` mints a bearer note under the
    payment preimage.

    `verify` (LUD-21, optional) lets a wallet with no node of its own poll
    settlement status - omitted unless VERIFY_ENABLED is set.

    `disposable` (LUD-11) tells a WALLET whether the payRequest LNURL/
    lightning address itself (not this one invoice) is meant to be kept
    around and reused - always `false` here: `/p` and the LUD-16 address
    are this mint's permanent, repeatable way to mint a fresh note, not a
    one-shot link that stops working after this payment. Per LUD-11, a
    WALLET that doesn't recognize this field at all is required to treat
    a link as disposable by default and may discard it - `false` must be
    sent explicitly, omitting it would silently undo that."""

    pr: str
    routes: list = []
    verify: str | None = None
    disposable: bool = False


class LnurlPayVerifyResponse(BaseModel):
    """LUD-21: settlement status for an invoice minted via `/p/cb`.

    `preimage`, once settled, IS the freshly minted bearer note's spend
    secret (see LUD-25's Minting a bearer note from a payRequest) - unlike
    a plain LUD-21 proof-of-payment, a wallet with no node of its own needs
    it to claim and immediately rotate the note: this mint's own node is a
    permanent prior holder of a freshly minted note's secret (it generated
    that preimage to fund the invoice in the first place - the one case
    where LUD-25's WALLET-generates-the-secret rule can't apply, since the
    secret has to come from the payment itself), so deferring the rotate
    leaves that exposure window open regardless of how promptly
    settlement was checked. `status`/`settled`/`pr` otherwise behave
    exactly per LUD-21."""

    status: Literal["OK"] = "OK"
    settled: bool
    preimage: str | None = None
    pr: str


class LnurlWithdrawResponse(BaseModel):
    """LUD-03 withdrawRequest for a single bearer note - min equals max
    equals the note's value, which is how a wallet reads a note's worth
    (this GET is informational and never burns anything).

    `mintPubkey` (LUD-25 Offline verification, optional) is this mint's
    signing key - omitted entirely if no funding source is configured
    (see signing.mint_pubkey)."""

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: str
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: str | None = None


class LnurlMintAddressResponse(BaseModel):
    """Theoretical companion to LUD-16 lnaddress (get_lnaddress): advertised
    at .well-known/lnurlw/{username}, this is the withdraw-side mirror of
    the mint's payRequest identity, completing the loop that
    LnurlPayResponse.withdrawLink starts. There's no k1 and no real balance
    behind `{username}` to draw from - this mint only ever custodies bearer
    notes, never per-user accounts (see README) - so unlike `/w`, this is
    purely informational: this mint's own node identity/capacity (see
    node.NodeInfo) plus the amount bounds a freshly minted note can fall
    into, and `payLink` (this mint's own LUD-16 address) so a wallet that
    resolves mint@host on its withdraw side still finds its way back to
    actually minting a note. `callback` points at the real `/w` withdraw
    endpoint for symmetry with LUD-03's shape, but with no k1 to append a
    wallet gets nothing more than "Unknown note" there - never a way to
    draw on this mint's own funds."""

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: str | None = None
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: str | None = None
    payLink: str
    nodeAlias: str | None = None
    nodeUri: str | None = None
    nodeColor: str | None = None
    nodeCapacityMsat: int | None = None


class WithdrawSuccessResponse(BaseModel):
    """LUD-03 success response, extended per LUD-25 (see
    router.get_withdraw_callback for the full melt/rotate/split/merge
    semantics). `pr`/`verify` echo a melt's invoice and its LUD-21-style
    settlement-proof URL, present only when VERIFY_ENABLED. `sig`/`sig2`
    are this mint's Offline-verification signatures over a rotate/split/
    merge's `h`/`h2` (see signing.sign_note) - `sig2` only for a split,
    both omitted if no funding source is configured. None fields are
    excluded on the wire."""

    status: Literal["OK"] = "OK"
    sig: str | None = None
    sig2: str | None = None
    pr: str | None = None
    verify: str | None = None
