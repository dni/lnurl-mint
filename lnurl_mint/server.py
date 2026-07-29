import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .frontend import frontend_router
from .router import router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # LUD-XX: a bearer note's k1 lives in the query string of /withdraw and
    # /withdraw/cb for as long as the note is held - unlike an ephemeral
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
