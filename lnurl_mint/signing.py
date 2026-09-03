import logging
from hashlib import sha256

from coincurve import PublicKey

from .node import LightningBackendConfig, fetch_node_info, sign_message

# LUD-25 Offline verification: signed via this mint's own funding-source
# identity. For lnd/cln that's the node's signmessage RPC, which always
# wraps the message with this prefix and double-sha256s it before signing -
# not a raw digest over a bespoke scheme, so any tool that already verifies
# a Lightning node's signed messages can verify a note too. Same reuse
# LUD-13 (../luds/13.md) relies on for LNURL-auth seed generation, rather
# than a separate keypair. The spark backend has no signmessage RPC and its
# SDK signs a different (single-sha256) digest it cannot redirect - so
# instead it signs this exact digest locally with a dedicated key derived
# from the wallet's own seed (m/25'/0'/0', see spark._sign_message_spark).
# LUD-25 only RECOMMENDS the node-id key ("SERVICE MAY sign notes with that
# same call"), and a spark wallet's invoices are signed by its SSP anyway;
# a wallet verifies a note by recovering the key from (digest, sig) and
# comparing it to the advertised mintPubkey, which holds for any secp256k1
# key - the spec-digest and wire format below are identical for all three
# backends, only the key differs.
_LIGHTNING_SIGNED_MESSAGE_PREFIX = b"Lightning Signed Message:"
_DOMAIN_TAG = "LNURLcash"


def lightning_signed_message_digest(message: str) -> bytes:
    """The LUD-25 note-signature digest: the standard "Lightning Signed
    Message" double-sha256 wrap (identical to what lnd's and cln's
    signmessage RPCs compute internally, and to what a WALLET recomputes
    to verify a note offline - see verify_note). Shared by the spark
    backend, which unlike lnd/cln has no signmessage RPC to produce it and
    so signs this digest locally with a dedicated seed-derived key (see
    spark._sign_message_spark)."""
    return sha256(sha256(_LIGHTNING_SIGNED_MESSAGE_PREFIX + message.encode()).digest()).digest()


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
    """This mint's offline-verification signing key (LUD-25 `mintPubkey`) -
    the funding source node's own identity pubkey for lnd/cln (the same
    key it signs BOLT-11 invoices with, so freshly minted and rotated
    notes verify against the same identity, exactly as the spec
    recommends), and for spark the dedicated seed-derived signing key its
    notes are signed with (see spark._lud25_signing_key - a purely local
    derivation, no network round trip). None if no funding source is
    configured or it's unreachable - offline verification is then simply
    unavailable, the same way funding-source-backed features are when
    that's unconfigured."""
    if not config.backend:
        return None
    if config.backend == "spark":
        from .spark import signing_pubkey_hex

        return signing_pubkey_hex(config)
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
    """A recoverable signature over (note_id_hex, amount_msat) per LUD-25's
    Offline verification, signed by the funding source node's own
    signmessage RPC, as 65 bytes (r, then s, then recovery id),
    hex-encoded. `note_id_hex` is the note's own hash - per LUD-25, the
    `h`/`h2` a WALLET generated and disclosed for a rotate/split/merge, so
    this mint signs exactly what it was given, never a secret it derived
    itself. The signmessage RPC itself returns recovery-id-leading bytes;
    per LUD-25 those are reordered here into r ‖ s ‖ recovery-id before
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
    digest = lightning_signed_message_digest(_message(note_id_hex, amount_msat))
    recovered = PublicKey.from_signature_and_message(signature, digest, hasher=None)
    return recovered.format(compressed=True).hex() == pubkey_hex
