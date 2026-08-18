"""Fetches the Swagger UI assets served at /docs into lnurl_mint/static/.

The assets are deliberately NOT committed to the repo (no static blobs in
git) and deliberately NOT loaded from a CDN at runtime (a third-party script
would execute on the mint's origin, and docs readers' IPs would leak to the
CDN) - so they are downloaded here, at build time, from a pinned version of
swagger-ui-dist with a pinned sha256 per file. The integrity guarantee is
identical to vendoring: bytes that don't match the pinned hash fail the
build loudly. The tradeoff vs vendoring is availability - the CDN must be
reachable at build time (files already present with matching hashes are
skipped, so repeat builds and rebuilds need no network).

Run via `make static`, or directly: `python3 scripts/fetch_swagger_ui.py`
(stdlib only, any python3). Wired into the Dockerfile, CI's test job, and
nix/package.nix (as fixed-output fetchurl derivations - the nix build
sandbox has no network, so bump the hashes there too when updating).

To update: bump SWAGGER_UI_VERSION and the sha256 of each asset below. The
original 5.32.13 hashes were cross-verified byte-identical between jsdelivr
and unpkg; do the same for a new version before trusting its hashes.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

SWAGGER_UI_VERSION = "5.32.13"

ASSETS = {
    "swagger-ui-bundle.js": "5f3be5d9cf40cdd60dca0dafeaf8743fd858d1b3bb717bbdaebf7201303f63d7",
    "swagger-ui.css": "9e617d9ac0afb0e430c11a17366de8624db7ce34c99ebd297443f0048ce30899",
}

STATIC_DIR = Path(__file__).parent.parent / "lnurl_mint" / "static"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    for name, pinned in ASSETS.items():
        dest = STATIC_DIR / name
        if dest.exists() and sha256(dest.read_bytes()) == pinned:
            print(f"{name}: already present, hash matches - skipping")
            continue
        url = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/{name}"
        print(f"{name}: fetching {url}")
        data = urllib.request.urlopen(url).read()
        if sha256(data) != pinned:
            sys.exit(
                f"{name}: sha256 mismatch - expected {pinned}, got {sha256(data)}. "
                "Refusing to write; if the version was just bumped, verify the new bytes "
                "against a second mirror (e.g. unpkg) and update ASSETS."
            )
        dest.write_bytes(data)
        print(f"{name}: wrote {len(data)} bytes, hash verified")


if __name__ == "__main__":
    main()
