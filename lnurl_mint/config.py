from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .node import LightningBackendConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # this mint's own funding source, configured once by the operator - used
    # to create the invoices that mint bearer notes and to pay the invoices
    # that melt them. Only the credential for the chosen backend is required
    # (macaroon for lnd, rune for cln); the other is ignored.
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

    # the one-pager frontend (GET /)
    title: str = "lnurl-mint"
    description: str = "A minimal lnurlcash mint - pay the QR code to mint a Lightning bearer note."
    # public base URL of this mint (e.g. https://mint.example) - used for the
    # QR code's LNURL, the lightning address domain, and the LUD-16 metadata
    # identifier. Falls back to each request's own base URL when unset.
    base_url: str | None = None
    # LUD-16: the mint is payable at {username}@{base_url host}
    username: str = "mint"

    def public_base_url(self, request_base_url: str) -> str:
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
