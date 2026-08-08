import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .node import LightningBackendConfig


class Settings(BaseSettings):
    # env_file is overridable via LNURL_MINT_ENV_FILE (pointed at a
    # nonexistent path by tests/conftest.py) so the test suite never picks
    # up a developer's own .env in this same directory (e.g. real
    # FUNDINGSOURCE_* credentials for local testing against lnurl_server's
    # regtest nodes) - a real process env var can't cleanly cancel out a
    # dotenv value (env_ignore_empty only skips *that* source, falling
    # through to the next-lowest, i.e. right back to the dotenv file), so
    # the file itself must not be read at all instead.
    model_config = SettingsConfigDict(
        env_file=os.environ.get("LNURL_MINT_ENV_FILE", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # this mint's own funding source, configured once by the operator - used
    # to create the invoices that mint bearer notes and to pay the invoices
    # that melt them, and to sign notes for LUD-XX's optional Offline
    # verification via the node's own signmessage RPC (see signing.py) -
    # there's no separate setting for that, it's simply unavailable without
    # a funding source. Only the credential for the chosen backend is
    # required (macaroon for lnd, rune for cln); the other is ignored.
    fundingsource_backend: Literal["lnd", "cln"] | None = None
    fundingsource_url: str | None = None
    fundingsource_macaroon: SecretStr | None = None
    fundingsource_rune: SecretStr | None = None
    # path to a self-signed TLS cert to verify the funding source against -
    # both lnd's and cln's REST APIs are commonly self-signed. Leave unset if
    # it's fronted by a reverse proxy with a real certificate.
    fundingsource_cert_path: str | None = None

    # bounds on the value of a single minted note (LUD-06 min/maxSendable)
    min_sendable_msat: int = 10_000
    max_sendable_msat: int = 1_000_000_000

    # LUD-XX's optional mint fee: withheld from every minted note's value
    # (a flat base_fee_msat plus fee_percent_ppm parts-per-million of the
    # amount paid), meant to cover the routing cost of eventually paying
    # the note back out on melt. Advertised in /p/cb's payRequest metadata
    # (see router._pay_response) so a wallet can warn the payer up front -
    # omitted from metadata entirely (assumed fee-free per spec) when both
    # are zero.
    base_fee_msat: int = 1000
    fee_percent_ppm: int = 0

    # floor on a note's value net of the mint fee (not on `amount` itself,
    # which min_sendable_msat already bounds) - guards against minting
    # dust-value notes not worth the routing cost of ever melting them.
    # /p/cb rejects an `amount` that would net less than this after fees.
    min_mint_msat: int = 10_000

    # cap on the number of k1s a single /w/cb request (melt/rotate/split/
    # merge) may name - well above any real wallet's outstanding note count
    # (one holding more consolidates across multiple requests), just to
    # bound the DB lookups - and query-string size - a single
    # unauthenticated request can force
    max_k1s: int = 100

    database_path: str = "mint.db"

    # LUD-21 (optional): advertise a `verify` URL in /p/cb's response, so a
    # wallet with no node of its own can poll whether its invoice settled.
    # Off by default - see router.verify_invoice for why this mint never
    # returns the spec's `preimage` field regardless of this setting.
    verify_enabled: bool = False

    # the one-pager frontend (GET /)
    title: str = "lnurl-mint"
    description: str = "A minimal lnurlcash mint - pay the QR code to mint a Lightning bearer note."
    # public base URL of this mint (e.g. https://mint.example) - used for the
    # QR code's LNURL, the lightning address domain, and the LUD-16 metadata
    # identifier. Required, not derived from a request's own Host header:
    # trusting that would let whoever sends the request (or, behind a cache
    # that doesn't vary on Host, an attacker poisoning a cached response for
    # other visitors) control the callback/withdrawLink URLs handed back to
    # a wallet.
    base_url: str
    # this mint's Tor hidden service address (e.g. http://<v3-address>.onion),
    # if it has one - advertised on the frontend one-pager as an alternative
    # way to reach it (see frontend.py). If a wallet is actually connecting
    # through this address (the request's own Host matches its hostname),
    # public_base_url prefers it over base_url, so the LNURL/callback URLs
    # in that response stay reachable over Tor - a fixed clearnet base_url
    # would otherwise leak into a Tor visitor's QR code and break payment
    # for them, since the callback would point back at a host Tor can't
    # reach (or that defeats the point of using Tor to begin with).
    onion_url: str | None = None
    # LUD-16: the mint is payable at {username}@{base_url host}
    username: str = "mint"

    def public_base_url(self, request_base_url: str) -> str:
        if self.onion_url:
            request_host = urlparse(request_base_url).hostname or ""
            onion_host = urlparse(self.onion_url).hostname or ""
            if request_host and request_host == onion_host:
                return self.onion_url.rstrip("/")
        return self.base_url.rstrip("/")

    def funding_source(self) -> LightningBackendConfig:
        return LightningBackendConfig(
            backend=self.fundingsource_backend,
            url=self.fundingsource_url,
            macaroon=self.fundingsource_macaroon,
            rune=self.fundingsource_rune,
            cert_path=self.fundingsource_cert_path,
        )


settings = Settings()
