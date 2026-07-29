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
    payment preimage."""

    pr: str
    routes: list = []


class LnurlWithdrawResponse(BaseModel):
    """LUD-03 withdrawRequest for a single bearer note - min equals max
    equals the note's value, which is how a wallet reads a note's worth
    (this GET is informational and never burns anything)."""

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: str
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""


class WithdrawSuccessResponse(BaseModel):
    """LUD-03 success response, extended per lnurlcash: `k1` is the newly
    minted bearer secret replacing the burned one(s) (rotate/merge/split),
    `change` the remainder note after a split. A melt (pr given) carries
    neither - None fields are excluded on the wire."""

    status: Literal["OK"] = "OK"
    k1: str | None = None
    change: str | None = None
