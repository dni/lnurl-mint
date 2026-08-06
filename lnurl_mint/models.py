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
    settlement status - omitted unless VERIFY_ENABLED is set."""

    pr: str
    routes: list = []
    verify: str | None = None


class LnurlPayVerifyResponse(BaseModel):
    """LUD-21: settlement status for an invoice minted via `/p/cb`.

    `preimage`, once settled, IS the freshly minted bearer note's spend
    secret (see LUD-XX's Minting a bearer note from a payRequest) - unlike
    a plain LUD-21 proof-of-payment, a wallet with no node of its own needs
    it to claim and immediately rotate the note (per the spec's Security
    considerations: `SERVICE`'s own node is a permanent prior holder of a
    freshly minted note's secret, so deferring the rotate leaves that
    exposure window open regardless of how promptly settlement was
    checked). `status`/`settled`/`pr` otherwise behave exactly per
    LUD-21."""

    status: Literal["OK"] = "OK"
    settled: bool
    preimage: str | None = None
    pr: str


class LnurlWithdrawResponse(BaseModel):
    """LUD-03 withdrawRequest for a single bearer note - min equals max
    equals the note's value, which is how a wallet reads a note's worth
    (this GET is informational and never burns anything).

    `mintPubkey` (LUD-XX Offline verification, optional) is this mint's
    signing key - omitted entirely if no funding source is configured
    (see signing.mint_pubkey)."""

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: str
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: str | None = None


class WithdrawSuccessResponse(BaseModel):
    """LUD-03 success response, extended per lnurlcash: `k1` is the newly
    minted bearer secret replacing the burned one(s) (rotate/merge/split),
    `change` the remainder note after a split. A melt (pr given) carries
    neither - None fields are excluded on the wire.

    `signature`/`changeSignature` (LUD-XX Offline verification, optional)
    are recoverable signatures over `k1`/`change` respectively, letting a
    holder verify the note offline against `mintPubkey` without contacting
    this mint - omitted (like `k1`/`change`) whenever there's no new note,
    and omitted entirely if no funding source is configured (see
    signing.sign_note)."""

    status: Literal["OK"] = "OK"
    k1: str | None = None
    change: str | None = None
    signature: str | None = None
    changeSignature: str | None = None
