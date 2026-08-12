import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .frontend import frontend_router
from .node import fetch_node_info
from .router import reconcile_pending_melts, router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # LUD-XX: a bearer note's k1 lives in the query string of /w and
    # /w/cb for as long as the note is held - unlike an ephemeral
    # LUD-03 k1, that can be a long time, turning access logs into a
    # durable theft vector (see the spec's "Secrets in GET query strings").
    # uvicorn's default access log records the full request line, query
    # string included, for every route - disabled here rather than scoped
    # to just those two, since nothing below the ASGI app can tell
    # uvicorn's access logger apart per route. An operator wanting access
    # logs for the rest should add them at a reverse proxy in front of this
    # app, which is the layer the spec assigns this same responsibility to.
    #
    # This must happen here, in a startup hook, not at module import time:
    # both `fastapi run` and `fastapi dev` reconfigure "uvicorn.access"
    # themselves as part of their own startup sequence, which runs after
    # this module is imported but before requests are served - disabling
    # it at import time gets silently undone by that later reconfiguration.
    logging.getLogger("uvicorn.access").disabled = True

    # a misconfigured or unreachable funding source degrades every
    # funding-source-backed feature (minting, melting, LUD-XX offline
    # verification) silently and per-request rather than failing outright
    # (see signing.mint_pubkey/sign_note, router._funding_source) - that's
    # the right behavior for a request, but an operator should still find
    # out from the logs at boot, not from a wallet failing to mint hours
    # later. This check is purely diagnostic: it changes no runtime
    # behavior, and every route still probes the funding source fresh on
    # its own.
    funding_source = settings.funding_source()
    if not funding_source.backend:
        logging.warning(
            "No funding source configured (FUNDINGSOURCE_BACKEND unset) - "
            "minting, melting, and offline verification are all unavailable."
        )
    else:
        try:
            info = await fetch_node_info(funding_source)
        except Exception as exc:
            logging.warning(
                f"Configured {funding_source.backend} funding source is unreachable at startup: {exc!s}. "
                "Minting, melting, and offline verification will be unavailable until it responds."
            )
        else:
            pubkey = info.uri.split("@")[0] if info.uri else "unknown pubkey"
            logging.info(
                f"Connected to {funding_source.backend} funding source: {info.alias or 'no alias'} ({pubkey})."
            )
            # a note left pending by a melt whose outcome never resolved
            # before this process last stopped would otherwise reject every
            # callback with "pending" forever - resolve what we now can
            # while the funding source is confirmed reachable (see
            # router.reconcile_pending_melts); only reachable here, not in
            # the branches above, since it needs a working funding_source
            await reconcile_pending_melts(funding_source)

    yield


app = FastAPI(
    title="lnurl-mint",
    description="Minimal lnurlcash (LUD-XX, Lightning bearer assets) mint - LUD-03/LUD-06 only.",
    lifespan=lifespan,
)

# every endpoint here is a public LNURL wire-protocol endpoint, meant to be
# fetched cross-origin by arbitrary third-party wallets; none reads a
# cookie, so a wide-open origin is safe
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

app.include_router(router)
app.include_router(frontend_router)
