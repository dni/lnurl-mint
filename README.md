# lnurl-mint

Minimal backend implementing **lnurlcash** ([LUD-XX](../luds/XX.md)), Lightning bearer
assets on top of plain [LUD-03](../luds/03.md) `withdrawRequest` and
[LUD-06](../luds/06.md) `payRequest`. A stripped-down sibling of
[lnurl_server](../lnurl_server); nothing but the mint.

A bearer note is a `k1` this mint has credited with value. It is minted by paying a
LUD-06 invoice (the payment preimage *is* the note), circulates offline as
`lnurlw://<host>/withdraw?k1=<k1>`, and can be rotated, split, merged, or melted back
to a BOLT-11 payment.

## Endpoints

| Endpoint        | Role                                                                          |
|-----------------|-------------------------------------------------------------------------------|
| `GET /`         | one-pager frontend: mint QR code (LNURL of `/pay`), lightning address, node info |
| `GET /pay`      | LUD-06 payRequest, extended with `withdrawLink` (the mint advertisement)      |
| `GET /pay/cb`   | LUD-06 callback, invoice whose preimage becomes a note once paid             |
| `GET /withdraw` | LUD-03 withdrawRequest for a note (`?k1=`), informational, never burns       |
| `GET /withdraw/cb` | the mutating callback: melt (`pr`), rotate, split (`amount`), merge (many `k1`) |
| `GET /.well-known/lnurlp/{username}` | LUD-16 alias for `/pay`, the mint is payable at `{USERNAME}@{BASE_URL host}` |

Callback semantics (`/withdraw/cb`):

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

**Not implemented:** the spec's optional offline-verification extension
(`mintPubkey` + recoverable signatures on rotate/split/merge, letting a note be
verified without contacting the mint). This mint's rotate/split/merge responses
carry no `signature`/`changeSignature`, and `mintPubkey` is not advertised.

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

## Test

```sh
uv run pytest
```
