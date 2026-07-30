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
    min_sendable_msat: int = 1000
    max_sendable_msat: int = 1_000_000_000

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
    # identifier. Falls back to each request's own base URL when unset.
    base_url: str | None = None
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

    # NORD (nostr ordinals, optional - see nostr.py): this mint's own
    # nostr signing key, 32 bytes hex. Note signatures (signing.py) are
    # delegated to the funding source node's signmessage RPC, but a nostr
    # event needs a raw BIP-340 signature no such RPC can produce - so
    # asset events are signed with this dedicated key instead, advertised
    # as `nostrPubkey` on the withdrawRequest. Unset = the whole asset
    # layer is dormant: notes are plain lnurlcash, exactly as before.
    nostr_secret_key: SecretStr | None = None
    # comma-separated relay URLs (wss://...) the outbox publisher drains
    # to - the first one doubles as the relay hint inside asset pointers
    nostr_relays: str | None = None

    def relay_list(self) -> list[str]:
        if not self.nostr_relays:
            return []
        return [relay.strip() for relay in self.nostr_relays.split(",") if relay.strip()]

    def public_base_url(self, request_base_url: str) -> str:
        if self.onion_url:
            request_host = urlparse(request_base_url).hostname or ""
            onion_host = urlparse(self.onion_url).hostname or ""
            if request_host and request_host == onion_host:
                return self.onion_url.rstrip("/")
        return (self.base_url or request_base_url).rstrip("/")

    def funding_source(self) -> LightningBackendConfig:
        return LightningBackendConfig(
            backend=self.fundingsource_backend,
            url=self.fundingsource_url,
            macaroon=self.fundingsource_macaroon,
            rune=self.fundingsource_rune,
            cert_path=self.fundingsource_cert_path,
        )


settings = Settings()
