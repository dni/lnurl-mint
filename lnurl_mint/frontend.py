import html
import re
from pathlib import Path
from string import Template
from urllib.parse import urlparse

import qrcode
import qrcode.image.svg
from bech32 import bech32_encode, convertbits
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import __version__
from .config import settings
from .node import fetch_node_info

frontend_router = APIRouter()

# Served locally at /favicon.svg rather than linked from lnurl-wallet's
# GitHub Pages, so the page has no third-party asset dependency.
FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"


def lnurl_encode(url: str) -> str:
    """LUD-01: bech32-encode a URL as an LNURL - uppercased, so the QR code
    can use the denser alphanumeric mode."""
    data = convertbits(url.encode(), 8, 5, True)
    assert data is not None
    return bech32_encode("lnurl", data).upper()


def _qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return image.to_string(encoding="unicode")


_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


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
  :root { color-scheme: dark; }
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
  }
  .qr svg { display: block; width: 100%; height: auto; }
  .copy {
    display: block; width: 100%; margin-bottom: .75rem; padding: .6rem .8rem;
    background: #1d2028; color: #e6e4dd; border: 1px solid #2c303b; border-radius: 8px;
    font-family: ui-monospace, monospace; font-size: .8rem; cursor: pointer;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .copy:hover { border-color: #4a5060; }
  .copy.copied { border-color: #7a9a65; }
  p.hint { color: #9a978f; font-size: .85rem; margin-top: 1.5rem; line-height: 1.5; }
  p.hint a { color: #e6e4dd; }
  p.hint code { font-family: ui-monospace, monospace; color: #e6e4dd; }
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
       color: #9a978f; margin: 1.5rem 0 .5rem; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  td { padding: .35rem 0; border-top: 1px solid #2c303b; text-align: left; }
  td:first-child { color: #9a978f; white-space: nowrap; padding-right: 1rem; }
  td.mono { font-family: ui-monospace, monospace; word-break: break-all; }
  td.value-row { display: flex; align-items: center; justify-content: space-between; gap: .5rem; }
  td.value-row span { word-break: break-all; }
  .copy-sm {
    flex: none; display: inline-flex; align-items: center; justify-content: center;
    width: 1.6rem; height: 1.6rem; padding: 0;
    background: #1d2028; color: #e6e4dd; border: 1px solid #2c303b; border-radius: 6px;
    font-size: .8rem; line-height: 1; cursor: pointer;
  }
  .copy-sm:hover { border-color: #4a5060; }
  .copy-sm.copied { border-color: #7a9a65; }
  .color-swatch {
    display: inline-block; padding: .15rem .5rem; border-radius: 6px;
    font-family: ui-monospace, monospace; font-size: .8rem;
  }
  .muted { color: #9a978f; font-size: .85rem; }
  footer { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #2c303b;
           font-size: .8rem; color: #9a978f; }
  footer a { color: #9a978f; text-decoration: underline; }
  footer a:hover { color: #e6e4dd; }
</style>
</head>
<body>
<main>
  <h1>$title</h1>
  <p class="desc">$description</p>
  <div class="qr">$qr_svg</div>
  <button class="copy" data-copy="$lnurl" title="Copy LNURL">$lnurl</button>
  <button class="copy" data-copy="$address" title="Copy lightning address">&#9889; $address</button>
  <p class="hint">
    After paying, redeem your bearer note at
    <a href="https://dni.github.io/lnurl-wallet" target="_blank" rel="noopener">dni.github.io/lnurl-wallet</a>
    &mdash; enter the mint's lightning address (<code>$address</code>) and the payment preimage.
  </p>
  $tor_section
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
    <tr><td>Connect string</td><td class="mono value-row"><span>$connect_string</span>$connect_copy</td></tr>
    <tr><td>Channels</td><td class="mono">$num_channels</td></tr>
    <tr><td>Peers</td><td class="mono">$num_peers</td></tr>
  </table>"""
)

COLOR_ROW = Template(
    """<tr><td>Color</td><td><span class="color-swatch" style="background:$color;color:$text_color">$color</span></td></tr>"""
)

COPY_SM = Template("""<button class="copy-sm" data-copy="$value" title="$title">&#10697;</button>""")


def _tor_section(base: str) -> str:
    """An alternative LNURL/address for this mint's Tor hidden service
    (ONION_URL), shown only when configured - and only when the current
    request isn't already using it, since public_base_url already returns
    the onion URL as the primary `base` in that case (see config.py), which
    would make a second, identical block here redundant."""
    onion_url = settings.onion_url
    if not onion_url or base.rstrip("/") == onion_url.rstrip("/"):
        return ""
    onion_base = onion_url.rstrip("/")
    onion_host = urlparse(onion_base).hostname or onion_base
    onion_lnurl = lnurl_encode(f"{onion_base}/p")
    return TOR_SECTION.substitute(
        qr_svg=_qr_svg(onion_lnurl),
        lnurl=onion_lnurl,
        address=html.escape(f"{settings.username}@{onion_host}"),
    )


def _copy_button(value: str | None, title: str) -> str:
    if not value:
        return ""
    return COPY_SM.substitute(value=html.escape(value), title=title)


async def _node_section() -> tuple[str, str | None]:
    """Returns the Node table's HTML, plus its validated `#rrggbb` color (if
    any) - the latter reused by index() for the page's <head> theme-color
    meta tag, so a wallet/browser chrome matching this mint's node color
    doesn't require a second round-trip to the funding source."""
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return '<p class="muted">No funding source configured.</p>', None
    try:
        node = await fetch_node_info(funding_source)
    except Exception:
        return '<p class="muted">Funding source node is unreachable.</p>', None
    # node.uri is "pubkey@host:port" once the node has an announced address,
    # or just the bare pubkey otherwise (see node._fetch_node_info_lnd/cln) -
    # split so the pubkey and the full connect string each get their own row.
    pubkey, _, host = (node.uri or "").partition("@")
    connect_string = node.uri if host else None
    color = node.color if node.color and _HEX_COLOR_RE.fullmatch(node.color) else None
    color_row = COLOR_ROW.substitute(color=color, text_color=_contrast_text_color(color)) if color else ""
    section = NODE_SECTION.substitute(
        alias=html.escape(node.alias or "-"),
        color_row=color_row,
        pubkey=html.escape(pubkey or "-"),
        pubkey_copy=_copy_button(pubkey, "Copy public key"),
        connect_string=html.escape(connect_string or "-"),
        connect_copy=_copy_button(connect_string, "Copy connect string"),
        num_channels=node.num_channels,
        num_peers=node.num_peers,
    )
    return section, color


@frontend_router.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(FAVICON_PATH, media_type="image/svg+xml")


@frontend_router.get("/", include_in_schema=False)
async def index(req: Request) -> HTMLResponse:
    """The one-pager: the mint's LNURL QR code (scan and pay to mint a
    bearer note), its lightning address, and the funding-source node info."""
    base, host = settings.public_base_url_and_host(str(req.base_url))
    lnurl = lnurl_encode(f"{base}/p")
    address = f"{settings.username}@{host}"
    node_section, color = await _node_section()
    theme_color_meta = f'<meta name="theme-color" content="{color}">' if color else ""
    page = PAGE.substitute(
        title=html.escape(settings.title),
        description=html.escape(settings.description),
        qr_svg=_qr_svg(lnurl),
        lnurl=lnurl,
        address=html.escape(address),
        tor_section=_tor_section(base),
        theme_color_meta=theme_color_meta,
        node_section=node_section,
        version=html.escape(__version__),
    )
    return HTMLResponse(page)
