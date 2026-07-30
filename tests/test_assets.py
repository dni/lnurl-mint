"""The NORD asset layer (nostr.py, db.py's asset tables, router's guards):
queued assets claim settling mints and get a genesis, rotate carries the
asset and records a transfer, melt closes the chain, split/merge are
refused - and none of it exists at all without NOSTR_SECRET_KEY.

Each test queues assets at its own distinct amount: the test database is
shared across this whole session (see conftest), and claim_asset matches
by exact value, so distinct amounts keep tests from claiming each other's
leftovers."""

import json
from hashlib import sha256

import pytest
from coincurve import PrivateKey
from coincurve.keys import PublicKeyXOnly
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lnurl_mint.assets import main as assets_main
from lnurl_mint.config import settings
from lnurl_mint.db import notes
from lnurl_mint.nostr import KIND_GENESIS, KIND_MELT, KIND_TRANSFER

NORD_SECRET = "60" * 32
NORD_PUBKEY = PrivateKey(bytes.fromhex(NORD_SECRET)).public_key.format(compressed=True)[1:].hex()


@pytest.fixture
def nord(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "nostr_secret_key", SecretStr(NORD_SECRET))
    monkeypatch.setattr(settings, "nostr_relays", "wss://relay.test")


def outbox_events(kind: int) -> list[dict]:
    return [
        event
        for _, event_json in notes.unpublished_events(limit=1000)
        if (event := json.loads(event_json))["kind"] == kind
    ]


def mint_asset_note(client: TestClient, mint_note, amount_msat: int, **asset_fields) -> str:
    """Queue one asset at `amount_msat`, mint a note of that value, and
    materialize it with the informational GET (settlement is lazy - the
    first /w resolves, settles, and claims)."""
    notes.queue_asset(
        asset_fields.get("content", '{"name":"Test Card"}'),
        asset_fields.get("artwork_url"),
        asset_fields.get("artwork_sha256"),
        asset_fields.get("collection"),
        amount_msat,
    )
    k1 = mint_note(amount_msat)
    assert client.get(f"/w?k1={k1}").json()["tag"] == "withdrawRequest"
    return k1


def test_plain_note_carries_no_nord_fields(client: TestClient, mint_note) -> None:
    k1 = mint_note(21_000)
    body = client.get(f"/w?k1={k1}").json()
    assert "asset" not in body
    assert "artwork" not in body
    assert "nostrPubkey" not in body


def test_no_asset_without_nostr_key(client: TestClient, mint_note) -> None:
    # queued, matching value - but the layer is dormant without a key, so
    # the asset stays queued and the note is plain cash
    notes.queue_asset('{"name":"Dormant"}', None, None, None, 22_000)
    k1 = mint_note(22_000)
    body = client.get(f"/w?k1={k1}").json()
    assert "asset" not in body


def test_settled_mint_claims_asset_and_publishes_genesis(nord, client: TestClient, mint_note) -> None:
    k1 = mint_asset_note(
        client,
        mint_note,
        15_000,
        content='{"name":"Signal Commons","set":"FASTPLAY SIGNAL"}',
        artwork_url="https://blossom.example/ab12",
        artwork_sha256="ab" * 32,
        collection="600b",
    )
    body = client.get(f"/w?k1={k1}").json()
    assert body["nostrPubkey"] == NORD_PUBKEY
    assert body["artwork"] == ["https://blossom.example/ab12", "ab" * 32]
    genesis_id, hint = body["asset"]
    assert hint == "wss://relay.test"

    (genesis,) = [e for e in outbox_events(KIND_GENESIS) if e["id"] == genesis_id]
    tags = {tag[0]: tag[1:] for tag in genesis["tags"]}
    # birth anchors the asset to the settled invoice: payment hash == the
    # note id == sha256 of the preimage the buyer holds
    assert tags["birth"][0] == sha256(bytes.fromhex(k1)).hexdigest()
    assert tags["amount"][0] == "15000"
    assert tags["x"][0] == "ab" * 32
    assert tags["t"][0] == "600b"
    assert json.loads(genesis["content"])["name"] == "Signal Commons"

    # the id is the real NIP-01 hash and the signature is real BIP-340
    serialized = json.dumps(
        [0, genesis["pubkey"], genesis["created_at"], genesis["kind"], genesis["tags"], genesis["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert sha256(serialized.encode()).hexdigest() == genesis["id"]
    PublicKeyXOnly(bytes.fromhex(genesis["pubkey"])).verify(bytes.fromhex(genesis["sig"]), bytes.fromhex(genesis["id"]))


def test_rotate_carries_asset_and_records_transfer(nord, client: TestClient, mint_note) -> None:
    k1 = mint_asset_note(client, mint_note, 16_000)
    genesis_id = client.get(f"/w?k1={k1}").json()["asset"][0]

    claimer = "ee" * 32
    rotated = client.get(f"/w/cb?k1={k1}&claimer={claimer}").json()
    new_k1 = rotated["k1"]
    assert client.get(f"/w?k1={new_k1}").json()["asset"][0] == genesis_id

    (transfer,) = [e for e in outbox_events(KIND_TRANSFER) if [genesis_id] in [t[1:2] for t in e["tags"]]]
    tags = {tuple(tag[-1:])[0] if tag[0] == "e" else tag[0]: tag for tag in transfer["tags"]}
    assert tags["genesis"][1] == genesis_id
    assert tags["prev"][1] == genesis_id  # first hop: prev is the genesis itself
    assert tags["p"][1] == claimer

    # the tip moved: a second rotate links to the transfer, not the genesis
    second = client.get(f"/w/cb?k1={new_k1}").json()["k1"]
    assert client.get(f"/w?k1={second}").json()["asset"][0] == genesis_id
    transfers = [e for e in outbox_events(KIND_TRANSFER) if e["tags"][0][1] == genesis_id]
    assert len(transfers) == 2
    prev_of_second = [t for t in transfers[-1]["tags"] if t[-1] == "prev"][0][1]
    assert prev_of_second == transfer["id"]


def test_asset_note_refuses_split_and_merge(nord, client: TestClient, mint_note) -> None:
    k1 = mint_asset_note(client, mint_note, 17_000)
    split = client.get(f"/w/cb?k1={k1}&amount=1000").json()
    assert split["status"] == "ERROR"
    assert "indivisible" in split["reason"]

    plain = mint_note(17_500)
    merge = client.get(f"/w/cb?k1={k1}&k1={plain}").json()
    assert merge["status"] == "ERROR"
    assert "merged" in merge["reason"]

    # both survived the refused operations
    assert client.get(f"/w?k1={k1}").json()["maxWithdrawable"] == 17_000
    assert client.get(f"/w?k1={plain}").json()["maxWithdrawable"] == 17_500


def test_melt_closes_the_chain(nord, client: TestClient, node, mint_note) -> None:
    from tests.conftest import fake_invoice

    k1 = mint_asset_note(client, mint_note, 18_000)
    genesis_id = client.get(f"/w?k1={k1}").json()["asset"][0]
    melted = client.get(f"/w/cb?k1={k1}&pr={fake_invoice(18_000)}").json()
    assert melted["status"] == "OK"
    (melt,) = [e for e in outbox_events(KIND_MELT) if e["tags"][0][1] == genesis_id]
    assert [t for t in melt["tags"] if t[-1] == "prev"][0][1] == genesis_id


def test_claimer_must_be_x_only_hex(nord, client: TestClient, mint_note) -> None:
    k1 = mint_asset_note(client, mint_note, 19_000)
    response = client.get(f"/w/cb?k1={k1}&claimer=npub1notanhexkey").json()
    assert response["status"] == "ERROR"
    assert "claimer" in response["reason"]
    # nothing was burned by the rejected request
    assert client.get(f"/w?k1={k1}").json()["maxWithdrawable"] == 19_000


def test_amount_mismatch_leaves_asset_queued(nord, client: TestClient, mint_note) -> None:
    notes.queue_asset('{"name":"Waiting"}', None, None, None, 20_000)
    k1 = mint_note(20_500)
    assert "asset" not in client.get(f"/w?k1={k1}").json()


def test_import_cli_queues_counted_instances(tmp_path) -> None:
    set_file = tmp_path / "set.json"
    set_file.write_text(
        json.dumps(
            {
                "assets": [
                    {"content": {"name": "Res"}, "amount_msat": 23_000, "count": 3},
                    {"content": {"name": "Ava"}, "amount_msat": 23_500, "artwork_sha256": "cd" * 32},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert assets_main(["import", str(set_file)]) == 0
    queued = notes.conn.execute("SELECT COUNT(*) FROM assets WHERE amount_msat IN (23000, 23500)").fetchone()[0]
    assert queued == 4
