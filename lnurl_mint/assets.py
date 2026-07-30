"""Asset set import - the one-time tool that queues pre-committed assets
so settling mint invoices claim them (router._assign_queued_asset) and a
genesis gets published per instance (see nostr.py):

    uv run python -m lnurl_mint.assets import path/to/set.json

The file shape:

    {"assets": [{
        "content": {...},              # genesis content, verbatim (collection-defined schema)
        "amount_msat": 15000,          # a settling mint of exactly this value claims it
        "artwork_url": "https://...",  # optional, with artwork_sha256 (Blossom-style: the
        "artwork_sha256": "<64 hex>",  #   hash is the commitment, the url only transport)
        "collection": "600b",          # optional `t` tag
        "count": 16                    # queue that many instances - count physical cards,
    }, ...]}                           #   one genesis each, not a display shorthand

Imports are additive and run against DATABASE_PATH like the server does -
run it on the same host (or the same mounted volume) as the mint."""

import json
import sys

from .db import notes


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "import":
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
    queued = 0
    for entry in data["assets"]:
        content = json.dumps(entry["content"], separators=(",", ":"), ensure_ascii=False)
        for _ in range(int(entry.get("count", 1))):
            notes.queue_asset(
                content,
                entry.get("artwork_url"),
                entry.get("artwork_sha256"),
                entry.get("collection"),
                int(entry["amount_msat"]),
            )
            queued += 1
    print(f"queued {queued} asset(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
