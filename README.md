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
uv run fastapi dev lnurl_mint/server.py
```

Configure the funding source via `.env` (see `.env.example`), lnd or cln REST.
Without one, minting and melting are unavailable (rotate/split/merge of existing
notes still work).

`make dev`/`make serve`/`make run` (Docker) default to port 8001, not 8000,
meant to run alongside a full `lnurl_server` instance on the same host, which
typically already claims 8000. Override with `make run PORT=...`.

## Docker

Pull the published image (see [Release](#release)) or build it locally:

```sh
docker pull dni256/lnurl-mint          # or: make build
```

Run it standalone - a plain bridge network, `.env` for configuration (copy
`.env.example` and fill it in), and a bind-mounted file so the sqlite
database survives container restarts:

```sh
mkdir -p data && touch data/mint.db
docker run --restart always -d --name lnurl-mint \
  -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/data/mint.db:/app/mint.db" \
  dni256/lnurl-mint
```

The container always listens on `8000` internally - map it to whichever host
port you want via `-p <host-port>:8000`. `FUNDINGSOURCE_CERT_PATH`, if you set
one, must point at a path that exists *inside* the container - if your lnd/cln
cert lives on the host, bind-mount it in too (`-v /host/path/tls.cert:/tls.cert:ro`
and point `FUNDINGSOURCE_CERT_PATH=/tls.cert` at the mounted path).

`make build`/`make run` wrap the same image for this repo's own local dev
setup specifically: `make run` joins a `lnurlserver_default` Docker network and
mounts certs from a sibling `lnurl_server` checkout so this mint can reach that
project's e2e regtest lnd/cln by internal hostname (see the Makefile's
`NETWORK`/`CERTS_DIR` and `.env`'s comments) - useful if you're developing
alongside that repo, but not a generic deployment recipe. For a real
deployment, adapt the standalone `docker run` above instead: put it on its own
network (or `--network host` if nothing else needs that port), point
`FUNDINGSOURCE_URL` at your own node, and front it with a reverse proxy for
TLS if it's reachable from the internet.

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
