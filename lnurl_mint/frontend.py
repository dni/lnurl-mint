import html
from string import Template
from urllib.parse import urlparse

import qrcode
import qrcode.image.svg
from bech32 import bech32_encode, convertbits
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .config import settings
from .node import fetch_node_info

frontend_router = APIRouter()


def lnurl_encode(url: str) -> str:
    """LUD-01: bech32-encode a URL as an LNURL - uppercased, so the QR code
    can use the denser alphanumeric mode."""
    data = convertbits(url.encode(), 8, 5, True)
    assert data is not None
    return bech32_encode("lnurl", data).upper()


def _qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return image.to_string(encoding="unicode")


PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
       color: #9a978f; margin: 1.5rem 0 .5rem; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  td { padding: .35rem 0; border-top: 1px solid #2c303b; text-align: left; }
  td:first-child { color: #9a978f; white-space: nowrap; padding-right: 1rem; }
  td.mono { font-family: ui-monospace, monospace; word-break: break-all; }
  .muted { color: #9a978f; font-size: .85rem; }
</style>
</head>
<body>
<main>
  <h1>$title</h1>
  <p class="desc">$description</p>
  <div class="qr">$qr_svg</div>
  <button class="copy" data-copy="$lnurl" title="Copy LNURL">$lnurl</button>
  <button class="copy" data-copy="$address" title="Copy lightning address">&#9889; $address</button>
  <h2>Node</h2>
  $node_section
</main>
<script>
  for (const el of document.querySelectorAll(".copy")) {
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

NODE_SECTION = Template(
    """<table>
    <tr><td>Alias</td><td class="mono">$alias</td></tr>
    <tr><td>URI</td><td class="mono">$uri</td></tr>
    <tr><td>Channels</td><td class="mono">$num_channels</td></tr>
    <tr><td>Peers</td><td class="mono">$num_peers</td></tr>
  </table>"""
)


async def _node_section() -> str:
    funding_source = settings.funding_source()
    if not funding_source.backend:
        return '<p class="muted">No funding source configured.</p>'
    try:
        node = await fetch_node_info(funding_source)
    except Exception:
        return '<p class="muted">Funding source node is unreachable.</p>'
    return NODE_SECTION.substitute(
        alias=html.escape(node.alias or "-"),
        uri=html.escape(node.uri or "-"),
        num_channels=node.num_channels,
        num_peers=node.num_peers,
    )


@frontend_router.get("/", include_in_schema=False)
async def index(req: Request) -> HTMLResponse:
    """The one-pager: the mint's LNURL QR code (scan and pay to mint a
    bearer note), its lightning address, and the funding-source node info."""
    base = settings.public_base_url(str(req.base_url))
    host = urlparse(base).hostname or req.url.hostname or "localhost"
    lnurl = lnurl_encode(f"{base}/pay")
    address = f"{settings.username}@{host}"
    page = PAGE.substitute(
        title=html.escape(settings.title),
        description=html.escape(settings.description),
        qr_svg=_qr_svg(lnurl),
        lnurl=lnurl,
        address=html.escape(address),
        node_section=await _node_section(),
    )
    return HTMLResponse(page)
