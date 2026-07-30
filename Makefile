.PHONY: all format lint check test black ruff checkblack checkruff mypy dev serve install build run

# 8001, not 8000: this is meant to run alongside a full lnurl_server
# instance on the same host, which already claims 8000
IMAGE_NAME = lnurl-mint
CONTAINER_NAME = lnurl-mint
PORT = 8001

# joins lnurl_server's own e2e regtest compose network (see
# ../lnurl_server/e2e/docker/docker-compose.yml, `name: lnurlserver`) so
# this container can reach its lnd/cln by internal hostname (lnd:8081,
# cln:3010) instead of localhost + --network host. CERTS_DIR is that same
# compose project's generated TLS material, bind-mounted read-only so
# FUNDINGSOURCE_CERT_PATH can point at it from inside the container - see
# .env's comments for the bare `make dev`/`make serve` (non-Docker)
# equivalents, which use localhost + the host cert path instead.
NETWORK = lnurlserver_default
CERTS_DIR = /home/user/repos/lnurl_server/e2e/docker/data

all: format lint
format: black ruff
lint: checkruff mypy
check: checkblack checkruff

black:
	uv run black lnurl_mint tests

ruff:
	uv run ruff check lnurl_mint tests --fix

checkruff:
	uv run ruff check lnurl_mint tests

checkblack:
	uv run black --check lnurl_mint tests

mypy:
	uv run mypy lnurl_mint

test:
	uv run pytest

install:
	uv sync

dev:
	FORWARDED_ALLOW_IPS=* \
	uv run fastapi dev lnurl_mint/server.py --host 0.0.0.0 --port $(PORT)

serve:
	FORWARDED_ALLOW_IPS=* \
	uv run fastapi run lnurl_mint/server.py --port $(PORT)

build:
	docker build --pull -t $(IMAGE_NAME) .

ENV_FILE := $(wildcard .env)

run:
	@echo "Restarting container..."
	docker stop $(CONTAINER_NAME) 2>/dev/null || true
	docker rm $(CONTAINER_NAME) 2>/dev/null || true
	mkdir -p data
	touch data/mint.db
	docker run --restart always -d --name $(CONTAINER_NAME) \
		--network $(NETWORK) \
		-p $(PORT):8000 \
		$(if $(ENV_FILE),--env-file $(ENV_FILE),) \
		-v $(CERTS_DIR):/lnurlserver-certs:ro \
		-v $(PWD)/data/mint.db:/app/mint.db \
		$(IMAGE_NAME)
	@echo "Container $(CONTAINER_NAME) is running at http://localhost:$(PORT)"
