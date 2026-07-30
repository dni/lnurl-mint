import json
import ssl
from base64 import b64decode, b64encode
from hashlib import sha256
from os import urandom
from typing import Literal

import bolt11
import httpx
from pydantic import BaseModel, SecretStr


async def _raise_for_status(res: httpx.Response) -> None:
    """Like httpx.Response.raise_for_status(), but folds the response body
    into the exception message - lnd and cln both put the actual failure
    reason there (e.g. cln's {"code": ..., "message": "Not permitted: ..."}
    or a rune's rejection reason), which the bare httpx exception otherwise
    discards, leaving only the status code visible in logs. Handles a
    streamed response (see _pay_invoice_lnd) that hasn't been read yet -
    safe to read fully here, since a non-2xx status means the caller was
    never going to consume it as a stream anyway."""
    if res.is_success:
        return
    try:
        body = res.text
    except httpx.ResponseNotRead:
        body = (await res.aread()).decode(errors="replace")
    try:
        res.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(f"{exc}: {body}", request=exc.request, response=exc.response) from exc


class LightningBackendConfig(BaseModel):
    """This mint's own funding source backend and credentials (configured
    once by the operator - see config.Settings.funding_source). Only the
    field(s) relevant to `backend` are ever read."""

    backend: Literal["lnd", "cln"] | None = None
    url: str | None = None
    macaroon: SecretStr | None = None  # lnd
    rune: SecretStr | None = None  # cln
    cert_path: str | None = None  # pin verification to a self-signed cert

    @property
    def verify(self) -> bool | ssl.SSLContext:
        if not self.cert_path:
            return True
        return ssl.create_default_context(cafile=self.cert_path)


async def create_invoice(
    amount_msat: int, config: LightningBackendConfig, memo: str = "lnurlcash mint"
) -> tuple[str, bytes]:
    if config.backend == "lnd":
        if not config.url or not config.macaroon:
            raise ValueError("Macaroon is required.")
        return await _create_invoice_lnd(amount_msat, config.url, config.macaroon.get_secret_value(), config, memo)
    if config.backend == "cln":
        if not config.url or not config.rune:
            raise ValueError("Rune is required.")
        return await _create_invoice_cln(amount_msat, config.url, config.rune.get_secret_value(), config, memo)
    raise ValueError(f"create_invoice is not supported for backend {config.backend!r}.")


async def pay_invoice(invoice: str, config: LightningBackendConfig) -> bytes:
    if config.backend == "lnd":
        if not config.url or not config.macaroon:
            raise ValueError("Macaroon is required.")
        return await _pay_invoice_lnd(invoice, config.url, config.macaroon.get_secret_value(), config)
    if config.backend == "cln":
        if not config.url or not config.rune:
            raise ValueError("Rune is required.")
        return await _pay_invoice_cln(invoice, config.url, config.rune.get_secret_value(), config)
    raise ValueError(f"pay_invoice is not supported for backend {config.backend!r}.")


async def sign_message(message: str, config: LightningBackendConfig) -> tuple[bytes, int]:
    """Signs `message` with this mint's own node identity key, via lnd's or
    cln's signmessage RPC - both follow the standard "Lightning Signed
    Message" convention (sign(sha256(sha256(b"Lightning Signed Message:" +
    message))), recoverable), the same one BOLT11-adjacent tools (LNbits,
    Zeus, ...) already use to prove node ownership. Neither backend exposes
    a way to sign an arbitrary raw digest instead - this wrapping is always
    applied. Returns (r || s, recovery_id)."""
    if config.backend == "lnd":
        if not config.url or not config.macaroon:
            raise ValueError("Macaroon is required.")
        return await _sign_message_lnd(message, config.url, config.macaroon.get_secret_value(), config)
    if config.backend == "cln":
        if not config.url or not config.rune:
            raise ValueError("Rune is required.")
        return await _sign_message_cln(message, config.url, config.rune.get_secret_value(), config)
    raise ValueError(f"sign_message is not supported for backend {config.backend!r}.")


async def is_invoice_settled(payment_hash: str, config: LightningBackendConfig) -> bool:
    """Whether an invoice this mint issued has been paid, checked against
    the funding source directly - callers should remember a True result
    locally (see db.NoteStore.settle_mint) so a settled invoice doesn't
    need to be re-queried."""
    if config.backend == "lnd":
        if not config.url or not config.macaroon:
            raise ValueError("Macaroon is required.")
        return await _is_invoice_settled_lnd(payment_hash, config.url, config.macaroon.get_secret_value(), config)
    if config.backend == "cln":
        if not config.url or not config.rune:
            raise ValueError("Rune is required.")
        return await _is_invoice_settled_cln(payment_hash, config.url, config.rune.get_secret_value(), config)
    raise ValueError(f"is_invoice_settled is not supported for backend {config.backend!r}.")


# --- lnd -----------------------------------------------------------------
# REST API: https://lightning.engineering/api-docs/api/lnd/


async def _create_invoice_lnd(
    amount_msat: int, url: str, macaroon: str, config: LightningBackendConfig, memo: str
) -> tuple[str, bytes]:
    # lnd's AddInvoice never returns the preimage - it lets the caller supply
    # one instead (r_preimage), so generate it ourselves and use that: the
    # preimage *is* the bearer note k1 once the invoice settles, so the mint
    # must know it for certain regardless of what the backend reports
    preimage = urandom(32)
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.post(
            f"{url}/v1/invoices",
            headers={"Grpc-Metadata-macaroon": macaroon},
            json={"value_msat": str(amount_msat), "memo": memo, "r_preimage": b64encode(preimage).decode()},
        )
        await _raise_for_status(res)
        payment_request = res.json().get("payment_request")
    if not payment_request:
        raise ValueError("lnd did not return a payment_request.")
    return payment_request, preimage


async def _pay_invoice_lnd(invoice: str, url: str, macaroon: str, config: LightningBackendConfig) -> bytes:
    # the non-streaming SendPaymentSync RPC is deprecated in favour of the
    # router's SendPaymentV2, which streams a Payment update per attempt
    # over chunked HTTP until a terminal SUCCEEDED/FAILED status
    async with httpx.AsyncClient(verify=config.verify, timeout=60.0) as client:
        async with client.stream(
            "POST",
            f"{url}/v2/router/send",
            headers={"Grpc-Metadata-macaroon": macaroon},
            json={
                "payment_request": invoice,
                "timeout_seconds": 60,
                "fee_limit_msat": str(_lnd_fee_limit_msat(invoice)),
            },
        ) as res:
            await _raise_for_status(res)
            payment = None
            async for line in res.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                payment = event.get("result", event)
                if payment.get("status") in ("SUCCEEDED", "FAILED"):
                    break

    if not payment or payment.get("status") != "SUCCEEDED":
        reason = (payment or {}).get("failure_reason", "unknown error")
        raise ValueError(f"Payment failed: {reason}")

    preimage_hex = payment.get("payment_preimage")
    if not preimage_hex:
        raise ValueError("lnd did not return a payment_preimage.")
    preimage = _decode_hex_or_base64(preimage_hex)
    _verify_preimage(preimage, invoice)
    return preimage


def _lnd_fee_limit_msat(invoice: str) -> int:
    amount_msat = bolt11.decode(invoice).amount_msat or 0
    # mirrors cln's pay defaults (0.5% maxfeepercent, 5000 msat exemptfee floor)
    return max(round(amount_msat * 0.005), 5000)


_ZBASE32_ALPHABET = "ybndrfg8ejkmcpqxot1uwisza345h769"


def _zbase32_decode(encoded: str) -> bytes:
    bits = "".join(f"{_ZBASE32_ALPHABET.index(c):05b}" for c in encoded.strip())
    whole_bytes = len(bits) // 8
    return bytes(int(bits[i : i + 8], 2) for i in range(0, whole_bytes * 8, 8))


async def _sign_message_lnd(message: str, url: str, macaroon: str, config: LightningBackendConfig) -> tuple[bytes, int]:
    """lnd's REST SignMessage - takes base64, returns a zbase32-encoded 65
    byte compact signature (1 header byte + r + s). lnd always treats its
    identity key as compressed, so header = 27 + recovery_id + 4."""
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.post(
            f"{url}/v1/signmessage",
            headers={"Grpc-Metadata-macaroon": macaroon},
            json={"msg": b64encode(message.encode()).decode()},
        )
        await _raise_for_status(res)
        signature_zbase32 = res.json().get("signature")
    if not signature_zbase32:
        raise ValueError("lnd did not return a signature.")
    raw = _zbase32_decode(signature_zbase32)
    if len(raw) != 65:
        raise ValueError(f"Unexpected lnd signature length: {len(raw)} bytes.")
    header, r, s = raw[0], raw[1:33], raw[33:65]
    return r + s, (header - 27) & 3


# --- cln -------------------------------------------------------------------
# clnrest REST plugin: https://docs.corelightning.org/docs/rest


async def _create_invoice_cln(
    amount_msat: int, url: str, rune: str, config: LightningBackendConfig, memo: str
) -> tuple[str, bytes]:
    # cln's `invoice` accepts a caller-supplied preimage too - same reasoning
    # as lnd above, generate it ourselves so we always know it for certain
    preimage = urandom(32)
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.post(
            f"{url}/v1/invoice",
            headers={"Rune": rune},
            json={
                "amount_msat": amount_msat,
                "label": urandom(16).hex(),
                "description": memo,
                "preimage": preimage.hex(),
            },
        )
        await _raise_for_status(res)
        bolt11_str = res.json().get("bolt11")
    if not bolt11_str:
        raise ValueError("cln did not return a bolt11 invoice.")
    return bolt11_str, preimage


async def _pay_invoice_cln(invoice: str, url: str, rune: str, config: LightningBackendConfig) -> bytes:
    async with httpx.AsyncClient(verify=config.verify, timeout=60.0) as client:
        res = await client.post(f"{url}/v1/pay", headers={"Rune": rune}, json={"bolt11": invoice})
        await _raise_for_status(res)
        payment = res.json()

    if payment.get("status") != "complete":
        raise ValueError(f"Payment failed: {payment.get('status', 'unknown error')}")

    preimage_hex = payment.get("payment_preimage")
    if not preimage_hex:
        raise ValueError("cln did not return a payment_preimage.")
    preimage = _decode_hex_or_base64(preimage_hex)
    _verify_preimage(preimage, invoice)
    return preimage


async def _sign_message_cln(message: str, url: str, rune: str, config: LightningBackendConfig) -> tuple[bytes, int]:
    """Core Lightning's clnrest plugin signmessage - already returns r||s
    and the recovery id separately (as hex), no zbase32 decoding needed;
    its `zbase` field is the same lnd-compatible encoding, unused here."""
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.post(f"{url}/v1/signmessage", headers={"Rune": rune}, json={"message": message})
        await _raise_for_status(res)
        result = res.json()
    signature_hex, recovery_id_hex = result.get("signature"), result.get("recid")
    if not signature_hex or recovery_id_hex is None:
        raise ValueError("cln did not return a signature.")
    return bytes.fromhex(signature_hex), int(recovery_id_hex, 16)


async def _is_invoice_settled_lnd(payment_hash: str, url: str, macaroon: str, config: LightningBackendConfig) -> bool:
    """lnd's REST LookupInvoice - r_hash_str takes the hex-encoded payment
    hash directly in the path."""
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.get(f"{url}/v1/invoice/{payment_hash}", headers={"Grpc-Metadata-macaroon": macaroon})
        await _raise_for_status(res)
        return bool(res.json().get("settled"))


async def _is_invoice_settled_cln(payment_hash: str, url: str, rune: str, config: LightningBackendConfig) -> bool:
    """Core Lightning's clnrest plugin listinvoices, filtered by payment_hash."""
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.post(f"{url}/v1/listinvoices", headers={"Rune": rune}, json={"payment_hash": payment_hash})
        await _raise_for_status(res)
        invoices = res.json().get("invoices") or []
    return bool(invoices) and invoices[0].get("status") == "paid"


class NodeInfo(BaseModel):
    """This mint's own funding-source node identity, shown on the one-pager
    frontend (GET /)."""

    alias: str | None = None
    uri: str | None = None  # node_key@host:port, or the bare pubkey if unannounced
    num_channels: int = 0
    num_peers: int = 0


async def fetch_node_info(config: LightningBackendConfig) -> NodeInfo:
    """A single getinfo against the funding source - both lnd and cln report
    identity, alias, and channel/peer counts in one call."""
    if config.backend == "lnd":
        if not config.url or not config.macaroon:
            raise ValueError("Macaroon is required.")
        return await _fetch_node_info_lnd(config.url, config.macaroon.get_secret_value(), config)
    if config.backend == "cln":
        if not config.url or not config.rune:
            raise ValueError("Rune is required.")
        return await _fetch_node_info_cln(config.url, config.rune.get_secret_value(), config)
    raise ValueError(f"fetch_node_info is not supported for backend {config.backend!r}.")


async def _fetch_node_info_lnd(url: str, macaroon: str, config: LightningBackendConfig) -> NodeInfo:
    """lnd's REST GetInfo - `uris` are already fully-formed "pubkey@host:port"
    strings, empty when the node has no announced address configured."""
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.get(f"{url}/v1/getinfo", headers={"Grpc-Metadata-macaroon": macaroon})
        await _raise_for_status(res)
        info = res.json()
    uris = info.get("uris") or []
    return NodeInfo(
        alias=info.get("alias") or None,
        uri=uris[0] if uris else info.get("identity_pubkey"),
        num_channels=int(info.get("num_active_channels", 0)) + int(info.get("num_inactive_channels", 0)),
        num_peers=int(info.get("num_peers", 0)),
    )


async def _fetch_node_info_cln(url: str, rune: str, config: LightningBackendConfig) -> NodeInfo:
    """Core Lightning's clnrest plugin GetInfo."""
    async with httpx.AsyncClient(verify=config.verify) as client:
        res = await client.post(f"{url}/v1/getinfo", headers={"Rune": rune})
        await _raise_for_status(res)
        info = res.json()
    node_id = info.get("id")
    uri = node_id
    addresses = info.get("address") or []
    if node_id and addresses and addresses[0].get("address") and addresses[0].get("port"):
        uri = f"{node_id}@{addresses[0]['address']}:{addresses[0]['port']}"
    return NodeInfo(
        alias=info.get("alias") or None,
        uri=uri,
        num_channels=int(info.get("num_active_channels", 0)) + int(info.get("num_inactive_channels", 0)),
        num_peers=int(info.get("num_peers", 0)),
    )


def _decode_hex_or_base64(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except ValueError:
        return b64decode(value)


def _verify_preimage(preimage: bytes, invoice: str) -> None:
    decoded = bolt11.decode(invoice)
    if decoded.has_payment_hash and sha256(preimage).hexdigest() != decoded.payment_hash:
        raise ValueError("Returned preimage does not match the invoice's payment hash.")
