"""NORD asset events - the nostr side of an asset mint (NORD-01/NORD-02,
https://github.com/BIMbeamFLX/nostr-ordinals): an asset's birth, custody
hops and death form a hash-linked chain of signed nostr events, sequenced
by this mint. Genesis (kind 7600) is published when a mint invoice
settles onto a queued asset, a transfer (7601) when an asset note
rotates, a melt (7603) when it is paid out.

Events are signed with the mint's own dedicated nostr key
(NOSTR_SECRET_KEY), not the funding source node's identity key: note
signatures (signing.py) go through the node's signmessage RPC, and no
such RPC can produce the raw BIP-340 signature a nostr event needs. The
withdrawRequest advertises `nostrPubkey` alongside `mintPubkey` - two
keys, one mint; every genesis carries this mint's lnurl, which is the
public link between them.

Publishing is strictly best-effort and asynchronous: every event is
persisted to the outbox (db.NoteStore.enqueue_event) in the same breath
as the note operation it describes, and publish_outbox_forever drains
that outbox to NOSTR_RELAYS whenever they are reachable. A note
operation never waits for, and never fails because of, a relay."""

import asyncio
import json
import logging
import time
from hashlib import sha256
from typing import Any

import websockets
from coincurve import PrivateKey

from .config import settings
from .db import notes

KIND_GENESIS = 7600
KIND_TRANSFER = 7601
KIND_MELT = 7603

# how often the outbox publisher wakes up to drain unpublished events
PUBLISH_INTERVAL_SECONDS = 10
# per-relay time budget: connect, send everything pending, read the OKs
RELAY_TIMEOUT_SECONDS = 10


def nostr_secret() -> PrivateKey | None:
    """The mint's nostr signing key, or None when the asset layer is
    dormant (unset or malformed NOSTR_SECRET_KEY - malformed warns, since
    an operator who set the variable expected it to work)."""
    secret = settings.nostr_secret_key
    if secret is None:
        return None
    try:
        raw = bytes.fromhex(secret.get_secret_value())
        if len(raw) != 32:
            raise ValueError
        return PrivateKey(raw)
    except ValueError:
        logging.warning("NOSTR_SECRET_KEY is not 32 bytes of hex - the NORD asset layer is disabled.")
        return None


def nostr_pubkey() -> str | None:
    """The x-only (BIP-340) pubkey events are verified against - what the
    withdrawRequest advertises as `nostrPubkey`."""
    key = nostr_secret()
    return key.public_key.format(compressed=True)[1:].hex() if key else None


def relay_hint() -> str:
    """The relay hint embedded in asset pointers and chain event tags -
    the first configured relay, or empty (a valid, if unhelpful, hint)."""
    relays = settings.relay_list()
    return relays[0] if relays else ""


def _event(key: PrivateKey, kind: int, tags: list[list[str]], content: str) -> dict[str, Any]:
    """A finalized nostr event: id per NIP-01 (sha256 over the canonical
    serialization), signature per BIP-340 over the id bytes."""
    pubkey = key.public_key.format(compressed=True)[1:].hex()
    created_at = int(time.time())
    serialized = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    event_id = sha256(serialized.encode()).hexdigest()
    return {
        "id": event_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": key.sign_schnorr(bytes.fromhex(event_id)).hex(),
    }


def genesis_event(
    key: PrivateKey,
    birth_payment_hash: str,
    amount_msat: int,
    content: str,
    artwork_url: str | None,
    artwork_sha256: str | None,
    collection: str | None,
) -> dict[str, Any]:
    """NORD-01 genesis (kind 7600). `birth` is the payment hash of the
    minting invoice - which, k1 being the preimage, equals the note's own
    storage id: the asset is anchored to the Lightning payment that
    charged it, verifiable by anyone holding the paid invoice."""
    tags: list[list[str]] = []
    if artwork_sha256:
        tags.append(["x", artwork_sha256])
    if artwork_url:
        tags.append(["url", artwork_url])
    tags.append(["birth", birth_payment_hash])
    tags.append(["amount", str(amount_msat)])
    if settings.base_url:
        tags.append(["lnurl", f"{settings.base_url.rstrip('/')}/w"])
    if collection:
        tags.append(["t", collection])
    return _event(key, KIND_GENESIS, tags, content)


def chain_event(
    key: PrivateKey, kind: int, genesis_id: str, prev_id: str, claimer: str | None = None
) -> dict[str, Any]:
    """NORD-01 transfer (7601) or melt (7603): one link, referencing the
    asset's genesis and the current chain tip. `claimer` is the npub a
    bearer receiver chose to disclose at rotate time - absent, the hop is
    recorded ownerless, exactly a banknote changing pockets."""
    hint = relay_hint()
    tags = [["e", genesis_id, hint, "genesis"], ["e", prev_id, hint, "prev"]]
    if claimer:
        tags.append(["p", claimer])
    return _event(key, kind, tags, "")


async def _drain_to_relay(relay: str, pending: list[tuple[str, str]]) -> set[str]:
    """Send every pending event to one relay, returning the ids it
    acknowledged (NIP-01 ["OK", id, true, ...])."""
    accepted: set[str] = set()
    async with websockets.connect(relay, open_timeout=RELAY_TIMEOUT_SECONDS) as ws:
        for event_id, event_json in pending:
            await ws.send(f'["EVENT",{event_json}]')
        for _ in pending:
            reply = json.loads(await asyncio.wait_for(ws.recv(), RELAY_TIMEOUT_SECONDS))
            if isinstance(reply, list) and len(reply) >= 3 and reply[0] == "OK" and reply[2]:
                accepted.add(reply[1])
    return accepted


async def publish_outbox_forever() -> None:
    """Background task (started from the server lifespan): every
    PUBLISH_INTERVAL_SECONDS, push unpublished outbox events to every
    configured relay. An event counts as published once any relay accepts
    it; relays that are down are simply retried next round. Every failure
    is swallowed - the outbox is durable, the relays are not our problem
    at note-operation time."""
    while True:
        try:
            pending = notes.unpublished_events()
            if pending:
                published: set[str] = set()
                for relay in settings.relay_list():
                    try:
                        published |= await _drain_to_relay(relay, pending)
                    except Exception as exc:
                        logging.debug(f"NORD publish to {relay} failed: {exc!s}")
                for event_id in published:
                    notes.mark_event_published(event_id)
        except Exception as exc:
            logging.warning(f"NORD outbox publisher error: {exc!s}")
        await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
