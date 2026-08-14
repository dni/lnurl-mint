import logging
from hashlib import sha256

from coincurve import PublicKey

from .node import LightningBackendConfig, fetch_node_info, sign_message

# LUD-XX Offline verification: signed via this mint's own funding-source
# node identity key (lnd's/cln's signmessage RPC - see node.sign_message),
# which always wraps the message with this prefix and double-sha256s it
# before signing - not a raw digest over a bespoke scheme, so any tool that
# already verifies a Lightning node's signed messages can verify a note too.
# Same reuse LUD-13 (../luds/13.md) relies on for LNURL-auth seed
# generation, rather than a separate keypair.
_LIGHTNING_SIGNED_MESSAGE_PREFIX = b"Lightning Signed Message:"
_DOMAIN_TAG = "LNURLcash"


def _message(note_id_hex: str, amount_msat: int) -> str:
    """The message a note's signature commits to. `note_id_hex` is
    sha256(k1) hex-encoded, never k1 itself - per LUD-25 it's exactly the
    `h`/`h2` a WALLET discloses on a rotate/split/merge callback (this
    mint's own note storage id, too), so a holder can prove issuance
    (e.g. to expose a mint that won't honor its own note) without
    revealing the spend secret, and this mint never needs to hash a raw
    secret to produce or verify a signature - it never has one to hash."""
    return f"{_DOMAIN_TAG}:{amount_msat}:{note_id_hex}"


async def mint_pubkey(config: LightningBackendConfig) -> str | None:
    """This mint's offline-verification signing key (LUD-XX `mintPubkey`) -
    the funding source node's own identity pubkey, the same one it signs
    BOLT-11 invoices with, so freshly minted and rotated notes verify
    against the same identity, exactly as the spec recommends. None if no
    funding source is configured or it's unreachable - offline verification
    is then simply unavailable, the same way funding-source-backed features
    are when that's unconfigured."""
    if not config.backend:
        return None
    try:
        info = await fetch_node_info(config)
    except Exception as exc:
        # not raised (offline verification is optional, see this
        # function's own docstring) but must not vanish with zero trace
        # either - see sign_note's own except below for the same reasoning
        logging.warning("mint_pubkey: could not reach %s funding source: %s", config.backend, exc)
        return None
    return info.uri.split("@")[0] if info.uri else None


async def sign_note(note_id_hex: str, amount_msat: int, config: LightningBackendConfig) -> str | None:
    """A recoverable signature over (note_id_hex, amount_msat) per LUD-XX's
    Offline verification, signed by the funding source node's own
    signmessage RPC, as 65 bytes (r, then s, then recovery id),
    hex-encoded. `note_id_hex` is the note's own hash - per LUD-25, the
    `h`/`h2` a WALLET generated and disclosed for a rotate/split/merge, so
    this mint signs exactly what it was given, never a secret it derived
    itself. The signmessage RPC itself returns recovery-id-leading bytes;
    per LUD-XX those are reordered here into r ‖ s ‖ recovery-id before
    being handed out, matching raw BOLT11 signatures. None if signing
    isn't possible right now (no funding source, or it's unreachable) -
    never raises, since a rotate/split/merge must still succeed without
    it."""
    if not config.backend:
        return None
    try:
        r_s, recovery_id = await sign_message(_message(note_id_hex, amount_msat), config)
    except Exception as exc:
        # not raised (see this function's own docstring) but must not
        # vanish with zero trace either - a signing RPC that always fails
        # (e.g. a macaroon/rune scoped without signmessage permission)
        # would otherwise look identical to "everything's fine, offline
        # verification is just turned off", indistinguishable from the
        # logs alone
        logging.warning("sign_note: could not sign via %s funding source: %s", config.backend, exc)
        return None
    return (r_s + bytes([recovery_id])).hex()


def verify_note(pubkey_hex: str, note_id_hex: str, amount_msat: int, signature_hex: str) -> bool:
    """Verifies a signature produced by sign_note against a mintPubkey - the
    check a WALLET performs offline, by reconstructing the same "Lightning
    Signed Message" digest lnd/cln computed internally when signing.
    `note_id_hex` is the note's hash (sha256(k1), hex) - the `h`/`h2` a
    real WALLET would already have on hand, since it generated the
    secret itself. This mint never calls it itself; it exists for the
    test suite to confirm sign_note produces what the spec's algorithm
    expects."""
    signature = bytes.fromhex(signature_hex)
    message = _message(note_id_hex, amount_msat).encode()
    digest = sha256(sha256(_LIGHTNING_SIGNED_MESSAGE_PREFIX + message).digest()).digest()
    recovered = PublicKey.from_signature_and_message(signature, digest, hasher=None)
    return recovered.format(compressed=True).hex() == pubkey_hex
