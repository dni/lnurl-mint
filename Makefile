.PHONY: all format lint check test black ruff checkblack checkruff mypy dev serve install build run

# 8001, not 8000: this is meant to run alongside a full lnurl_server
# instance on the same host, which already claims 8000
IMAGE_NAME = lnurl-mint
CONTAINER_NAME = lnurl-mint
PORT = 8001

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
		--network host \
		-e PORT=$(PORT) \
		$(if $(ENV_FILE),--env-file $(ENV_FILE),) \
		-v $(PWD)/data/mint.db:/app/mint.db \
		$(IMAGE_NAME)
	@echo "Container $(CONTAINER_NAME) is running at http://localhost:$(PORT)"
