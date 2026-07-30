# lnurl-mint

Minimal backend implementing **lnurlcash** ([LUD-XX](../luds/XX.md)), Lightning bearer
assets on top of plain [LUD-03](../luds/03.md) `withdrawRequest` and
[LUD-06](../luds/06.md) `payRequest`. A stripped-down sibling of
[lnurl_server](../lnurl_server); nothing but the mint.

A bearer note is a `k1` this mint has credited with value. It is minted by paying a
LUD-06 invoice (the payment preimage *is* the note), circulates offline as
`lnurlw://<host>/w?k1=<k1>`, and can be rotated, split, merged, or melted back
to a BOLT-11 payment.

## Endpoints

| Endpoint        | Role                                                                          |
|-----------------|-------------------------------------------------------------------------------|
| `GET /`         | one-pager frontend: mint QR code (LNURL of `/p`), lightning address, node info |
| `GET /p`      | LUD-06 payRequest, extended with `withdrawLink` (the mint advertisement)      |
| `GET /p/cb`   | LUD-06 callback, invoice whose preimage becomes a note once paid             |
| `GET /verify/{payment_hash}` | LUD-21, settlement status for an invoice minted via `/p/cb`      |
| `GET /w` | LUD-03 withdrawRequest for a note (`?k1=`), informational, never burns       |
| `GET /w/cb` | the mutating callback: melt (`pr`), rotate, split (`amount`), merge (many `k1`) |
| `GET /.well-known/lnurlp/{username}` | LUD-16 alias for `/p`, the mint is payable at `{USERNAME}@{BASE_URL host}` |

Callback semantics (`/w/cb`):

| `k1`  | `pr` | `amount` | Result                                                    |
|-------|------|----------|-----------------------------------------------------------|
| one   | yes  | –        | melt: note burned, `pr` (of exactly its value) paid       |
| one   | no   | no       | rotate: burned, fresh `k1'` of the same value returned    |
| one   | no   | yes      | split: burned, response carries `k1` (amount) + `change`  |
| many  | no   | –        | merge: all burned, one note worth the sum returned        |

`pr` MUST NOT be combined with multiple `k1`s or with `amount`, melt several notes
by merging them first. The informational endpoint's response always echoes the
literal secret it was queried with (never a derived id), and ignores an `amount`
query param if present, notes may encode a wallet-declared value in their URL
(`?k1=...&amount=...`) for offline display, but it is never authoritative;
`maxWithdrawable` is.

Per the spec's security considerations, no spendable secret is ever persisted: notes
are stored keyed by `sha256(k1)`, for a minted note that is exactly the payment
hash of the invoice that funded it, so the preimage is discarded at invoice-creation
time. The spec also asks `SERVICE` not to log query strings on the withdraw
endpoints, since a bearer note's `k1` can sit in one far longer than an ephemeral
LUD-03 `k1` would, this mint disables uvicorn's per-request access log entirely
(see `server.py`'s lifespan) rather than leave secrets in server logs by default;
run it behind a reverse proxy if you want access logs for the other routes.

**Offline verification** (optional): if a funding source is configured, `GET
/w` advertises a `mintPubkey` - that node's own identity, the same key
it signs BOLT-11 invoices with - and rotate/split/merge responses carry a
recoverable `signature`/`changeSignature` over each new note, letting a holder
verify a note's issuer and amount without contacting the mint (see
`signing.py`). Notes are signed via the funding source's own signmessage RPC
(lnd's `/v1/signmessage`, cln's `signmessage`), which both wrap the message
with the standard "Lightning Signed Message:" prefix and double-sha256 it
before signing - the same convention other Lightning tooling already uses to
prove node ownership, rather than a bespoke raw-digest scheme neither backend
can actually produce. There's no separate setting for this: without a funding
source, both fields are simply omitted, same as any other unconfigured
optional field, and signing failures (e.g. a briefly unreachable node) are
swallowed rather than failing the rotate/split/merge itself.

**Verify** (optional, [LUD-21](../luds/21.md)): set `VERIFY_ENABLED=true` to
advertise a `verify` URL in `/p/cb`'s response, letting a wallet with no node
of its own poll `/verify/{payment_hash}` for settlement status instead of
watching the invoice itself. Deviates from the spec in one deliberate way:
the response never includes `preimage`, even when settled. LUD-21's own
example response does return it, but for lnurlcash the preimage *is* the
bearer note's spend secret (see LUD-XX) - a `payment_hash` is not secret (an
invoice's own recipient can trivially derive it, and it may end up in logs,
proxies, or a wallet's own history), so handing back the preimage here would
let anyone who merely saw the invoice steal the note the instant it settles,
racing whoever actually paid for it. `/verify/{payment_hash}` itself always
works when hit directly; `VERIFY_ENABLED` only controls whether `/p/cb`
advertises it.

**Tor**: set `ONION_URL` to this mint's hidden service address (e.g.
`http://<v3-address>.onion`) to advertise it on the frontend one-pager as an
alternative way to reach the mint, alongside its clearnet QR/address. This
isn't just cosmetic: if a wallet is actually connecting through that address,
`ONION_URL` is used as the base for the LNURL/callback URLs *instead of*
`BASE_URL` (see `config.py`'s `public_base_url`) - otherwise a fixed clearnet
`BASE_URL` would leak into a Tor visitor's QR code, pointing their wallet's
callback at a host it can't (or shouldn't have to) reach, breaking payment
over Tor entirely. Running the hidden service itself is outside this app's
scope - point a Tor `HiddenServiceDir` (or an onion-services-capable reverse
proxy) at whatever host/port this mint is already listening on, the same way
you'd front it with Caddy/nginx for clearnet.

## NORD asset layer (optional)

Notes can be **assets** - nostr ordinals over lnurlcash, per the
[NORD drafts](https://github.com/BIMbeamFLX/nostr-ordinals): a unique,
indivisible note (a trading card, a ticket, a numbered banknote) whose
birth, custody hops and death this mint publishes as a hash-linked chain
of signed nostr events, with artwork content-addressed by sha256.

Set `NOSTR_SECRET_KEY` (a dedicated 32-byte-hex nostr key - note
signatures come from the funding source node's signmessage RPC, but a
nostr event needs a raw BIP-340 signature no such RPC can produce, so
asset events use this key, advertised as `nostrPubkey` on the
withdrawRequest) and `NOSTR_RELAYS`, then queue pre-committed assets:

```sh
uv run python -m lnurl_mint.assets import set.json
```

The next mint invoice settling for exactly an asset's value claims it: a
genesis event (kind `7600`, `birth` = the invoice's payment hash) goes to
the relays via a durable outbox, the withdrawRequest gains
`asset: [genesis id, relay]` and `artwork: [url, sha256]`, rotating the
note records a transfer (`7601`, with the receiver's npub iff they
disclosed one via the callback's `claimer` param), melting closes the
chain (`7603`). Asset notes refuse split and merge - an ordinal is
indivisible. Without the key, everything here is dormant and notes are
plain cash, exactly as before.

## Run

```sh
uv sync
uv run fastapi dev lnurl_mint/server.py
```

Configure the funding source via `.env` (see `.env.example`), lnd or cln REST.
Without one, minting and melting are unavailable (rotate/split/merge of existing
notes still work).

**cln rune**: this mint only ever calls `invoice`, `pay`, `signmessage`,
`listinvoices` and `getinfo` (see `node.py`), so scope `FUNDINGSOURCE_RUNE` to
just those instead of handing it a full-access rune:

```sh
lightning-cli createrune restrictions='[["method=invoice","method=pay","method=signmessage","method=listinvoices","method=getinfo"]]'
```

The command's JSON output's `rune` field is the value for `FUNDINGSOURCE_RUNE`.
The single `[...]` restriction is an OR list (any of these five methods, and
nothing else) - a comma-separated top-level list instead would AND further
restrictions on top (e.g. `pnum=0` to also disallow all requests with
parameters).

`make dev`/`make serve`/`make run` (Docker) default to port 8111, not 8000,
meant to run alongside a full `lnurl_server` instance on the same host, which
typically already claims 8000. Override with `make run PORT=...`.

## Docker

Pull the published image (see [Release](#release)) or build it locally:

```sh
docker pull dni256/lnurl-mint          # or: make build
```

`make build`/`make run` (or the equivalent `docker run` below) use
`--network host`, so the container shares the host's network namespace
directly - `FUNDINGSOURCE_URL=https://localhost:3010` in `.env` then reaches
a node running on the host exactly like a bare `make dev`/`make serve` would,
with no `host.docker.internal`/gateway-IP workaround needed. The tradeoff is
the usual one for host networking: no port remapping (the app listens on
`PORT` directly, see below) and no network isolation from the host.

```sh
mkdir -p data && touch data/mint.db
docker run --restart always -d --name lnurl-mint \
  --network host \
  -e PORT=8111 \
  --env-file .env \
  -v "$(pwd)/data/mint.db:/app/mint.db" \
  dni256/lnurl-mint
```

`PORT` (baked into the image as `ENV PORT=8111`, overridable at `docker run
-e PORT=...`) is what the app itself listens on - with `--network host` there's
no `-p host:container` mapping to remap a port with, so this is the only way
to choose it. `FUNDINGSOURCE_CERT_PATH`, if you set one, must point at a path
that exists *inside* the container - if your lnd/cln cert lives on the host,
bind-mount it in too (`-v /host/path/tls.cert:/tls.cert:ro` and point
`FUNDINGSOURCE_CERT_PATH=/tls.cert` at the mounted path).

`make build`/`make run` wrap exactly the recipe above (same `.env`, same
volume) - `make run PORT=...` picks the port, defaulting to 8111 (see `make
dev`'s docstring above for why). Prefer real network isolation instead (e.g.
deploying where the funding source is reachable over the network rather than
on `localhost`)? Drop `--network host` and use `-p <host-port>:8111` instead,
same as any other container. Front it with a reverse proxy for TLS if it's
reachable from the internet.

## Test

```sh
uv run pytest
```

## Release

Pushing a `v*` tag (`git tag v1.2.0 && git push origin v1.2.0`) triggers
`.github/workflows/release.yml`, which:

* builds the image and pushes `dni256/lnurl-mint` to Docker Hub, tagged `1.2.0`,
  `1.2`, `1`, and `latest`
* creates a GitHub Release for the tag (via `gh release create
  --generate-notes`), with notes auto-generated from the commits/PRs merged
  since the previous tag

The Docker push needs two repo secrets (Settings → Secrets and variables →
Actions):

* `DOCKERHUB_USERNAME` - the Docker Hub account/org to push under
* `DOCKERHUB_TOKEN` - an access token (not the account password), created at
  [hub.docker.com/settings/security](https://hub.docker.com/settings/security)

The GitHub Release needs no extra secret (just the default `GITHUB_TOKEN`).
