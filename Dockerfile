# Stage 1: install Python dependencies into a venv. Kept separate from the
# runtime stage so that if a future dependency ever needs a C toolchain to
# build, only this stage grows, not the shipped image. --no-install-project:
# the app is run straight from its source directory (see the runtime CMD),
# it's never needed as an installed package.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-cache --no-install-project


# Stage 2: Python runtime - no uv, no compilers, just the venv built above
# plus the app's own source
FROM python:3.12-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY lnurl_mint/ ./lnurl_mint/

ENV FORWARDED_ALLOW_IPS=*
ENV PATH="/app/.venv/bin:${PATH}"

# runtime settings (see lnurl_mint/config.py) are read from real
# environment variables - pass them with `docker run --env-file .env`
# (see .env.example for what's available), no file needs copying in.
# always 8000 internally - map to whatever host port you want with
# `docker run -p <host-port>:8000` (see the Makefile's own PORT for that)
EXPOSE 8000

CMD ["fastapi", "run", "lnurl_mint/server.py", "--host", "0.0.0.0", "--port", "8000"]
