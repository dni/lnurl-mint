# lnurl-mint

Minimal backend implementing **lnurlcash** ([LUD-XX](https://github.com/lnurl/luds/pull/301),
open PR), Lightning bearer assets on top of plain [LUD-03](../luds/03.md)
`withdrawRequest` and [LUD-06](../luds/06.md) `payRequest`. A stripped-down
sibling of [lnurl_server](../lnurl_server); nothing but the mint.

A bearer note is a `k1` this mint has credited with value. It is minted by paying a
LUD-06 invoice (the payment preimage *is* the note), circulates offline as
`lnurlw://<host>/w?k1=<k1>`, and can be rotated, split, merged, or melted back
to a BOLT-11 payment. Redeem one with [lnurl-wallet](https://github.com/dni/lnurl-wallet),
a reference wallet implementation (hosted at
[dni.github.io/lnurl-wallet](https://dni.github.io/lnurl-wallet)).

## Endpoints

| Endpoint        | Role                                                                          |
|-----------------|-------------------------------------------------------------------------------|
| `GET /`         | one-pager frontend: mint QR code (LNURL of `/p`), lightning address, node info |
| `GET /p`      | LUD-06 payRequest, extended with `withdrawLink` (the mint advertisement)      |
| `GET /p/cb`   | LUD-06 callback, invoice whose preimage becomes a note once paid - reports `disposable: false` ([LUD-11](../luds/11.md)): the lightning address itself is meant to be stored and reused |
| `GET /verify/{payment_hash}` | LUD-21, settlement status for an invoice minted via `/p/cb` or paid out by a melt via `/w/cb` ([LUD-25](../luds/25.md)) |
| `GET /w` | LUD-03 withdrawRequest for a note (`?k1=`), informational, never burns       |
| `GET /w/cb` | the mutating callback: melt (`pr`), rotate, split (`amount`), merge (many `k1`) |
| `GET /.well-known/lnurlp/{username}` | LUD-16 alias for `/p`, the mint is payable at `{USERNAME}@{BASE_URL host}` |

Callback semantics (`/w/cb`):

| `k1`  | `pr` | `amount` | Result                                                    |
|-------|------|----------|-----------------------------------------------------------|
| one   | yes  | –        | melt: note reserved, OK (plus `pr`/`verify` if verify is enabled) returned immediately, `pr` (of exactly its value) paid asynchronously, burned once settled |
| one   | no   | no       | rotate: burned, a note keyed by `h` (of the same value) minted |
| many  | no   | yes      | split: all burned, two notes minted - `amount` keyed by `h`, the remainder keyed by `h2` |
| many  | no   | –        | merge: all burned, one note worth the sum minted, keyed by `h` |

`pr` MUST NOT be combined with multiple `k1`s or with `amount`, melt several notes
by merging them first. The informational endpoint's response always echoes the
literal secret it was queried with (never a derived id), and ignores an `amount`
query param if present, notes may encode a wallet-declared value in their URL
(`?k1=...&amount=...`) for offline display, but it is never authoritative;
`maxWithdrawable` is.

**`h`/`h2`** ([LUD-25](../luds/25.md)): whenever `pr` is absent (rotate, split,
or merge), the caller (`WALLET`) - never this mint - generates the replacement
note's secret, a fresh random preimage, and discloses only its sha256 hash as
`h` (and, for a split's change note, `h2`). This mint registers the new note
under that hash directly and never sees, generates, or persists the underlying
preimage - the callback response for these carries no secret at all, just
`{"status": "OK"}` (plus `sig`/`sig2` if offline verification is configured,
see below). `h` is required whenever `pr` is absent; `h2` is additionally
required whenever `amount` is too. A missing or malformed one fails with
`{"status": "ERROR", "reason": "missing h"}` (or `"missing h2"`) rather than
this mint generating a secret on `WALLET`'s behalf.

Per the spec, `/w/cb` replies `{"status": "OK"}` for a melt as soon as the note
is reserved, then pays `pr` asynchronously in the background - it does not wait
for the outgoing payment to settle before responding. A melted `k1` MUST NOT be
burned until that payment actually settles, so for the duration of the (now
backgrounded) payment attempt its note is only reserved (`pending`), not yet
burned - any other callback naming that `k1` (another melt, a rotate, a split,
a merge) fails with `{"status": "ERROR", "reason": "pending"}` until it
resolves, at which point the note is either burned for good (payment settled)
or released back to outstanding (payment confirmed failed). Since the initial
response is sent before the payment is even attempted, a melt failure is never
reported back through this callback - only observable as the note becoming
spendable again.

No spendable secret is ever persisted or, for a rotate/split/merge, even seen
by this mint at all: notes are stored keyed by `sha256(k1)` - `h`/`h2` above,
supplied by `WALLET` directly - and for a freshly minted note that id is
exactly the payment hash of the invoice that funded it, so the preimage is
discarded at invoice-creation time. The spec also asks `SERVICE` not to log query strings on the withdraw
endpoints, since a bearer note's `k1` can sit in one far longer than an ephemeral
LUD-03 `k1` would, this mint disables uvicorn's per-request access log entirely
(see `server.py`'s lifespan) rather than leave secrets in server logs by default;
run it behind a reverse proxy if you want access logs for the other routes.

**Mint fee** (optional): set `BASE_FEE_MSAT`/`FEE_PERCENT_PPM` to withhold a
flat amount plus a parts-per-million cut of every mint's `amount`, credited
to `k1=P`'s note instead of the full amount paid - meant to cover the
routing cost of eventually paying that note back out on melt. Advertised as
an extra `["text/plain", "Mint fees: <base_fee_msat>,<fee_percent_ppm>"]`
entry in `/p`'s `metadata`, so a wallet that recognizes the `Mint fees: `
prefix can warn the payer up front; omitted entirely (assumed fee-free per
spec) when both are `0`. `MIN_MINT_MSAT` (default 10 sats) floors the note's
value net of this fee - not `amount` itself, which `MIN_SENDABLE_MSAT`
already bounds - so a mint too small to net a note worth minting is rejected
by `/p/cb` before an invoice is even created. The computed fee is always
rounded *up* to the nearest whole sat (never left at fractional-msat
precision), so the mint is never short a sat versus the naive estimate a
wallet derives from the metadata formula above.

**Offline verification** (optional): if a funding source is configured, `GET
/w` advertises a `mintPubkey` - that node's own identity, the same key
it signs BOLT-11 invoices with - and rotate/split/merge responses carry a
recoverable `sig`/`sig2` over each new note's hash (`h`/`h2`, supplied by
`WALLET` - this mint signs exactly what it was given, never a secret it
derived itself), letting a holder verify a note's issuer and amount without
contacting the mint (see `signing.py`). Notes are signed via the funding source's own signmessage RPC
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
watching the invoice itself. Once settled, the response's `preimage` *is* the
freshly minted bearer note's spend secret (see [LUD-XX](https://github.com/lnurl/luds/pull/301)) -
unlike a plain LUD-21 proof-of-payment, that wallet needs it to claim the note
at all, so it must be handed over despite `SERVICE`'s own node already being a
permanent prior holder of that same secret; the wallet MUST rotate the note
immediately after (see LUD-XX's Security considerations) rather than treat
verify as having closed that exposure window. `preimage` is fetched live from the
funding source on every call, never cached locally, same as every other
secret this mint handles. `/verify/{payment_hash}` itself always works when
hit directly; `VERIFY_ENABLED` only controls whether `/p/cb` advertises it.

The same flag extends a melt's own response the same way, per LUD-25: `pr`
(the invoice this melt is paying, echoed back) and `verify` (a `/verify/`
URL for it) are attached to `{"status": "OK"}` once the outgoing payment's
`payment_hash` is known, letting a `WALLET` prove a melt actually happened
without trusting this mint's word for it - a BOLT-11 `pr` commits to
`payment_hash = sha256(preimage)`, so anyone holding both `pr` and the
`preimage` `verify` eventually reports (fetched live, same as the mint
side) can check that independently. Unlike a fresh mint's `preimage`, a
melt's is never a bearer secret - the note(s) that funded it are already
burned by the time it's returned - so there's no analogous rotate-immediately
requirement here.

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

## Run

```sh
uv sync
FORWARDED_ALLOW_IPS=* uv run uvicorn lnurl_mint.server:app --reload
```

Configure the funding source via `.env` (see `.env.example`), lnd or cln REST.
Without one, minting and melting are unavailable (rotate/split/merge of existing
notes still work).

**cln rune**: this mint only ever calls `invoice`, `xpay`, `signmessage`,
`listinvoices`, `listpays` and `getinfo` (see `node.py`), so scope
`FUNDINGSOURCE_RUNE` to just those instead of handing it a full-access rune:

```sh
lightning-cli createrune restrictions='[["method=invoice","method=xpay","method=signmessage","method=listinvoices","method=listpays","method=getinfo"]]'
```

The command's JSON output's `rune` field is the value for `FUNDINGSOURCE_RUNE`.
The single `[...]` restriction is an OR list (any of these six methods, and
nothing else) - a comma-separated top-level list instead would AND further
restrictions on top (e.g. `pnum=0` to also disallow all requests with
parameters).

**lnd macaroon**: `admin.macaroon` works, but this mint only ever calls
`AddInvoice`/`LookupInvoice`, the router's `SendPaymentV2`/`TrackPaymentV2`,
`SignMessage`, and `GetInfo` (see `node.py`) - scope `FUNDINGSOURCE_MACAROON`
to just those instead of handing it full admin access:

```sh
lncli bakemacaroon invoices:write invoices:read offchain:write offchain:read message:write info:read --save_to=lnurl-mint.macaroon
```

Set `FUNDINGSOURCE_MACAROON` to the hex-encoded contents of that file
(`xxd -p -c1000 lnurl-mint.macaroon`, or drop `--save_to` to have `lncli`
print the hex directly instead of writing a file). `message:write` is the
one easy to leave out and the one that breaks quietly: without it,
`SignMessage` calls fail, and since offline verification (LUD-XX) is
optional and never blocks a rotate/split/merge on failure (see
`signing.sign_note`), a scoped-too-narrow macaroon shows up as every note
silently missing its signature rather than an obvious error - check the
logs for `sign_note: could not sign via lnd funding source: ...` if that
happens.

## Docker

```sh
mkdir -p data && touch data/mint.db
docker run --restart always -d --name lnurl-mint \
  --network host \
  --user "$(id -u):$(id -g)" \
  -e PORT=8111 \
  -e DATABASE_PATH=/app/data/mint.db \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  dni256/lnurl-mint          # or: make run
```

The image runs as a non-root user; `--user` matches it to whichever host
user owns `data/` so it can write `mint.db` and its sqlite journal/WAL
files (which must live in the *same directory*, not just the db file
itself - a plain `-v .../mint.db:/app/mint.db` file mount isn't enough).

`--network host` lets `FUNDINGSOURCE_URL=https://localhost:3010` in `.env`
reach a node running on the host directly, no `host.docker.internal`
workaround needed - the tradeoff is no port remapping (`PORT` picks what the
app listens on) and no network isolation. If your lnd/cln cert lives on the
host, bind-mount it in too (`-v /host/path/tls.cert:/tls.cert:ro`, then set
`FUNDINGSOURCE_CERT_PATH=/tls.cert`).

Prefer real network isolation? Drop `--network host` and use `-p
<host-port>:8111` instead, same as any other container. Front it with a
reverse proxy for TLS if it's reachable from the internet.

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
