import html
from pathlib import Path
from string import Template
from urllib.parse import urlparse

import qrcode
import qrcode.image.svg
from bech32 import bech32_encode, convertbits
from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse

from . import __version__
from .config import settings
from .db import notes
from .node import cached_fetch_node_info
from .router import max_mintable_msat

frontend_router = APIRouter()

# Served locally at /favicon.svg rather than linked from lnurl-wallet's
# GitHub Pages, so the page has no third-party asset dependency.
FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"

# Swagger UI, fetched at build time by scripts/fetch_swagger_ui.py (pinned
# swagger-ui-dist version + pinned sha256 there; gitignored, never committed)
# so /docs pulls nothing from a CDN - a third-party script would execute on
# this origin, and docs readers' IPs would leak to it.
SWAGGER_JS_PATH = Path(__file__).parent / "static" / "swagger-ui-bundle.js"
SWAGGER_CSS_PATH = Path(__file__).parent / "static" / "swagger-ui.css"


def lnurl_encode(url: str) -> str:
    """LUD-01: bech32-encode a URL as an LNURL - uppercased, so the QR code
    can use the denser alphanumeric mode."""
    data = convertbits(url.encode(), 8, 5, True)
    assert data is not None
    return bech32_encode("lnurl", data).upper()


def _qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return image.to_string(encoding="unicode")


def _contrast_text_color(hex_color: str) -> str:
    """A readable text color (near-black or near-white, matching the page's
    own dark/light text shades) for text placed on top of `hex_color` -
    plain YIQ brightness, good enough for a swatch nobody's staring at for
    accessibility compliance."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return "#14161c" if brightness > 140 else "#f5f4f0"


PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
$theme_color_meta
<title>$title</title>
<style>
  :root { color-scheme: dark; --mint: #2e9e6c; --mint-bright: #5fe3ac; }
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: system-ui, sans-serif;
    background: #14161c; color: #e6e4dd;
    min-height: 100vh; display: grid; place-items: center; padding: 2rem 1rem;
  }
  main { width: 100%; max-width: 26rem; text-align: center; }
  h1 { font-size: 1.5rem; margin-bottom: .5rem; }
  p.desc { color: #9a978f; font-size: .95rem; margin-bottom: 1.5rem; }
  .qr {
    background: #fff; border-radius: 12px; padding: 12px;
    width: 100%; max-width: 20rem; margin: 0 auto 1rem;
    border: 2px solid var(--mint);
  }
  .qr svg { display: block; width: 100%; height: auto; }
  .copy {
    display: block; width: 100%; margin-bottom: .75rem; padding: .6rem .8rem;
    background: #1d2028; color: #e6e4dd; border: 1px solid #2c303b; border-radius: 8px;
    font-family: ui-monospace, monospace; font-size: .8rem; cursor: pointer;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .copy:hover { border-color: var(--mint); }
  .copy.copied { border-color: var(--mint-bright); color: var(--mint-bright); }
  p.hint { color: #9a978f; font-size: .85rem; margin-top: 1.5rem; line-height: 1.5; }
  p.hint a { color: var(--mint-bright); }
  p.hint code { font-family: ui-monospace, monospace; color: #e6e4dd; }
  p.sunset-warning {
    margin-top: 0; margin-bottom: 1.5rem; padding: .6rem .8rem;
    background: rgba(230, 168, 80, .12); border: 1px solid #e6a850; border-radius: 8px;
    color: #e6a850; font-weight: 600;
  }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
       color: var(--mint); margin: 1.5rem 0 .5rem; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  td { padding: .35rem 0; border-top: 1px solid #2c303b; text-align: left; }
  td:first-child { color: #9a978f; white-space: nowrap; padding-right: 1rem; }
  td.mono { font-family: ui-monospace, monospace; word-break: break-all; }
  td.value-row { display: flex; align-items: center; justify-content: space-between; gap: .5rem; }
  td.value-row span { word-break: break-all; }
  td a { color: #e6e4dd; text-decoration: underline; }
  td a:hover { color: #fff; }
  .copy-sm {
    flex: none; display: inline-flex; align-items: center; justify-content: center;
    width: 1.6rem; height: 1.6rem; padding: 0;
    background: #1d2028; color: #e6e4dd; border: 1px solid #2c303b; border-radius: 6px;
    font-size: .8rem; line-height: 1; cursor: pointer;
  }
  .copy-sm:hover { border-color: var(--mint); }
  .copy-sm.copied { border-color: var(--mint-bright); color: var(--mint-bright); }
  .color-swatch {
    display: inline-block; padding: .15rem .5rem; border-radius: 6px;
    font-family: ui-monospace, monospace; font-size: .8rem;
  }
  .muted { color: #9a978f; font-size: .85rem; }
  footer { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #2c303b;
           font-size: .8rem; color: #9a978f; }
  footer a { color: #9a978f; text-decoration: underline; }
  footer a:hover { color: var(--mint-bright); }
</style>
</head>
<body>
<main>
  <h1>$title</h1>
  <p class="desc">$description</p>
  $sunset_warning
  $pay_section
  $tor_section
  $mint_section
  <h2>Outstanding notes</h2>
  $outstanding_section
  <h2>Node</h2>
  $node_section
  <footer>
    <a href="https://github.com/dni/lnurl-mint" target="_blank" rel="noopener">GitHub</a>
    &middot;
    <a href="/docs">API docs</a>
    &middot;
    v$version
  </footer>
</main>
<script>
  for (const el of document.querySelectorAll(".copy, .copy-sm")) {
    el.addEventListener("click", async () => {
      await navigator.clipboard.writeText(el.dataset.copy);
      el.classList.add("copied");
      setTimeout(() => el.classList.remove("copied"), 800);
    });
  }
</script>
</body>
</html>
"""
)

SUNSET_WARNING = Template(
    """<p class="hint sunset-warning">
    &#9888; This mint plans to stop operating on <strong>$date</strong>. Melt or migrate any notes before then.
  </p>"""
)

PAY_SECTION = Template(
    """<div class="qr">$qr_svg</div>
  <button class="copy" data-copy="$lnurl" title="Copy LNURL">$lnurl</button>
  <button class="copy" data-copy="$address" title="Copy lightning address">&#9889; $address</button>
  <p class="hint">
    Pay from an LNURLcash-aware wallet like
    <a href="https://wallet.lnurlcash.com" target="_blank" rel="noopener">wallet.lnurlcash.com</a>
    to mint a bearer note &mdash; it generates the note's secret and attaches it to the payment for you.
  </p>"""
)

# What stands in its place once sunset_mint is on. A QR code here would
# encode this mint's payRequest, and /p/cb refuses every one of those, so
# the page would be handing out a scan whose only outcome is an error. The
# note side is untouched by sunsetting, so this says what still works and
# how to reach it.
SUNSET_SECTION = Template(
    """<p class="hint">
    <strong>This mint is no longer issuing notes</strong>, so there is
    nothing here to scan or pay.
    <br><br>
    A note it issued earlier is unaffected: check it, rotate it, merge
    several into one, and melt into a Lightning invoice, all as before.
    Open the note in any LNURLcash wallet, or at
    <a href="https://wallet.lnurlcash.com" target="_blank" rel="noopener">wallet.lnurlcash.com</a>
    give this mint's address (<code>$address</code>) and the note's secret.
  </p>"""
)

MINT_SECTION = Template(
    """<h2>Mint</h2>
  $limits_section"""
)

TOR_SECTION = Template(
    """<h2>Also via Tor</h2>
  <div class="qr">$qr_svg</div>
  <button class="copy" data-copy="$lnurl" title="Copy LNURL">$lnurl</button>
  <button class="copy" data-copy="$address" title="Copy lightning address">&#9889; $address</button>"""
)

NODE_SECTION = Template(
    """<table>
    <tr><td>Alias</td><td class="mono">$alias</td></tr>
    $color_row
    <tr><td>Public key</td><td class="mono value-row"><span>$pubkey</span>$pubkey_copy</td></tr>
    $connect_rows
    <tr><td>Channels</td><td class="mono">$num_channels</td></tr>
    <tr><td>Peers</td><td class="mono">$num_peers</td></tr>
    <tr><td>Capacity</td><td class="mono">$capacity</td></tr>
    $explorers_row
  </table>"""
)

# One row per advertised address (see node.NodeInfo.uris) - a node with
# both a clearnet and a Tor address gets two of these, only the first
# carrying the row label so it reads as one "Connect string" entry with
# multiple values rather than several unrelated rows.
CONNECT_ROW = Template(
    """<tr><td>$label</td><td class="mono value-row"><span>$connect_string</span>$connect_copy</td></tr>"""
)

EXPLORERS_ROW = Template(
    """<tr><td>Explorers</td><td class="value-row">"""
    """<a href="https://mempool.space/lightning/node/$pubkey" target="_blank" rel="noopener">mempool.space</a>"""
    """<a href="https://amboss.space/node/$pubkey" target="_blank" rel="noopener">amboss.space</a>"""
    """</td></tr>"""
)

LIMITS_SECTION = Template(
    """<table>
    <tr><td>Min amount</td><td class="mono">$min_amount</td></tr>
    <tr><td>Max amount</td><td class="mono">$max_amount</td></tr>
  </table>"""
)

OUTSTANDING_SECTION = Template(
    """<table>
    <tr><td>Notes</td><td class="mono">$count</td></tr>
    <tr><td>Total value</td><td class="mono">$value</td></tr>
  </table>"""
)

COLOR_ROW = Template(
    """<tr><td>Color</td><td>"""
    """<span class="color-swatch" style="background:$color;color:$text_color">$color</span>"""
    """</td></tr>"""
)

COPY_SM = Template("""<button class="copy-sm" data-copy="$value" title="$title">&#10697;</button>""")


def _tor_section(base: str) -> str:
    """An alternative LNURL/address for this mint's Tor hidden service
    (ONION_URL), shown only when configured - and only when the current
    request isn't already using it, since public_base_url already returns
    the onion URL as the primary `base` in that case (see config.py), which
    would make a second, identical block here redundant. Empty while
    sunsetting, for the reason in _pay_section."""
    onion_url = settings.onion_url
    if not onion_url or base.rstrip("/") == onion_url.rstrip("/"):
        return ""
    # a sunsetting mint refuses /p/cb over the onion exactly as it does over
    # the clearnet, so this block would be the same dead invitation twice
    if settings.sunset_mint:
        return ""
    onion_base = onion_url.rstrip("/")
    onion_host = urlparse(onion_base).hostname or onion_base
    onion_lnurl = lnurl_encode(f"{onion_base}/.well-known/lnurlp/{settings.username}")
    return TOR_SECTION.substitute(
        qr_svg=_qr_svg(onion_lnurl),
        lnurl=onion_lnurl,
        address=html.escape(f"{settings.username}@{onion_host}"),
    )


def _copy_button(value: str | None, title: str) -> str:
    if not value:
        return ""
    return COPY_SM.substitute(value=html.escape(value), title=title)


def _format_sats(amount_msat: int) -> str:
    return f"{amount_msat // 1000:,} sats"


def _pay_section(lnurl: str, address: str) -> str:
    """The pay half of the page: QR code, LNURL, lightning address, and what
    to do once the invoice settles. A sunsetting mint gets SUNSET_SECTION
    instead - /p/cb rejects outright while sunset_mint is on (see
    router.mint), so rendering a QR code of this mint's payRequest would be
    offering the one thing the mint has stopped doing, and any wallet that
    scanned it would get an error for its trouble."""
    if settings.sunset_mint:
        return SUNSET_SECTION.substitute(address=html.escape(address))
    return PAY_SECTION.substitute(
        qr_svg=_qr_svg(lnurl),
        lnurl=lnurl,
        address=html.escape(address),
    )


def _sunset_warning() -> str:
    """A visible heads-up banner once SUNSET_DATE is configured (see
    config.py's own docstring) - shown independently of sunset_mint: the
    whole point is to warn holders *before* minting actually stops, not
    just report it once it already has, so this appears whether or not
    sunset_mint is on yet. Empty (no banner at all) when unset - most
    mints never plan to sunset."""
    if not settings.sunset_date:
        return ""
    return SUNSET_WARNING.substitute(date=settings.sunset_date.isoformat())


def _mint_section() -> str:
    """The Mint table, and its heading. Both describe amounts a new note may
    be minted for, so both go while sunsetting: there are no new notes, and
    a floor and a ceiling on an impossible action are just two more numbers
    to read past."""
    if settings.sunset_mint:
        return ""
    return MINT_SECTION.substitute(limits_section=_limits_section())


def _limits_section() -> str:
    """The Mint table's HTML: the amount bounds a freshly minted note can
    actually fall into - min_mint_msat (the floor a note's value must clear
    net of fees) and router.max_mintable_msat (the fee-adjusted ceiling: a
    note minted from the full max_sendable_msat still nets less than that
    raw setting whenever a mint fee is configured, same as the LUD-16
    address's own minSendable already accounts for on the floor side - see
    router._min_sendable_msat), the same two numbers advertised on the
    mint-address discovery endpoint (see router.get_mint_address)."""
    return LIMITS_SECTION.substitute(
        min_amount=_format_sats(settings.min_mint_msat),
        max_amount=_format_sats(max_mintable_msat()),
    )


def _outstanding_section() -> str:
    """The Outstanding table's HTML: how many bearer notes this mint has
    issued and never burned, and their combined value (NoteStore.
    outstanding_notes) - this mint's total liability, straight from its own
    database. Unlike the Node table below, this needs no funding source and
    is never hidden while sunsetting: a sunsetting mint still owes every
    outstanding note, and that's exactly when a holder most wants to see
    this number."""
    count, amount_msat = notes.outstanding_notes()
    return OUTSTANDING_SECTION.substitute(count=f"{count:,}", value=_format_sats(amount_msat))


async def _node_section() -> tuple[str, str | None]:
    """Returns the Node table's HTML, plus its validated `#rrggbb` color (if
    any) - the latter reused by index() for the page's <head> theme-color
    meta tag, so a wallet/browser chrome matching this mint's node color
    doesn't require a second round-trip to the funding source."""
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return '<p class="muted">No funding source configured.</p>', None
    try:
        node = await cached_fetch_node_info(funding_source)
    except Exception:
        return '<p class="muted">Funding source node is unreachable.</p>', None
    # node.uri is "pubkey@host:port" once the node has an announced address,
    # or just the bare pubkey otherwise (see node._fetch_node_info_lnd/cln) -
    # split so the pubkey and the full connect string(s) each get their own
    # row(s).
    pubkey, _, host = (node.uri or "").partition("@")
    # node.uris holds every announced address (a node behind Tor as well as
    # clearnet has more than one); falls back to the single node.uri when
    # uris wasn't populated (node.uri is always uris[0] when uris is
    # non-empty - see node.NodeInfo) - empty exactly when host is, i.e.
    # this node has nothing beyond a bare pubkey to connect to yet.
    connect_uris = node.uris or ([node.uri] if host and node.uri else [])
    if connect_uris:
        connect_rows = "".join(
            CONNECT_ROW.substitute(
                label="Connect string" if i == 0 else "",
                connect_string=html.escape(uri),
                connect_copy=_copy_button(uri, "Copy connect string"),
            )
            for i, uri in enumerate(connect_uris)
        )
    else:
        connect_rows = CONNECT_ROW.substitute(label="Connect string", connect_string="-", connect_copy="")
    # node.color is already validated as a well-formed "#rrggbb" or None
    # by node._normalize_color - substituted into the style attribute below
    # unescaped, so nothing malformed can reach this point in the first place
    color = node.color
    color_row = COLOR_ROW.substitute(color=color, text_color=_contrast_text_color(color)) if color else ""
    # only shown once there's an actual pubkey to link to - a node whose
    # uri is unknown (unreachable partway through, or truly bare) has
    # nothing for these explorers to look up
    explorers_row = EXPLORERS_ROW.substitute(pubkey=html.escape(pubkey)) if pubkey else ""
    section = NODE_SECTION.substitute(
        alias=html.escape(node.alias or "-"),
        color_row=color_row,
        pubkey=html.escape(pubkey or "-"),
        pubkey_copy=_copy_button(pubkey, "Copy public key"),
        connect_rows=connect_rows,
        num_channels=node.num_channels,
        num_peers=node.num_peers,
        explorers_row=explorers_row,
        capacity=_format_sats(node.capacity),
    )
    return section, color


@frontend_router.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(FAVICON_PATH, media_type="image/svg+xml")


@frontend_router.get("/static/swagger-ui-bundle.js", include_in_schema=False)
async def swagger_js() -> FileResponse:
    return FileResponse(SWAGGER_JS_PATH, media_type="application/javascript")


@frontend_router.get("/static/swagger-ui.css", include_in_schema=False)
async def swagger_css() -> FileResponse:
    return FileResponse(SWAGGER_CSS_PATH, media_type="text/css")


@frontend_router.get("/docs", include_in_schema=False)
async def docs(req: Request) -> HTMLResponse:
    """Replaces FastAPI's default /docs (disabled in server.py), which loads
    Swagger UI from jsdelivr; this one serves the local copy instead (see
    the SWAGGER_*_PATH comment above)."""
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{req.app.title} - Swagger UI",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
        swagger_favicon_url="/favicon.svg",
    )


@frontend_router.get("/", include_in_schema=False)
async def index(req: Request) -> HTMLResponse:
    """The one-pager: the mint's LNURL QR code (scan and pay to mint a
    bearer note), its lightning address, and the funding-source node info.
    While sunsetting, the mint half of that is replaced by what a holder of
    an existing note can still do - see _pay_section."""
    base, host = settings.public_base_url_and_host(str(req.base_url))
    lnurl = lnurl_encode(f"{base}/.well-known/lnurlp/{settings.username}")
    address = f"{settings.username}@{host}"
    node_section, color = await _node_section()
    theme_color_meta = f'<meta name="theme-color" content="{color}">' if color else ""
    page = PAGE.substitute(
        title=html.escape(settings.title),
        description=html.escape(settings.description),
        sunset_warning=_sunset_warning(),
        pay_section=_pay_section(lnurl, address),
        tor_section=_tor_section(base),
        theme_color_meta=theme_color_meta,
        mint_section=_mint_section(),
        outstanding_section=_outstanding_section(),
        node_section=node_section,
        version=html.escape(__version__),
    )
    return HTMLResponse(page)
