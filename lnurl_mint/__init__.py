import os
from importlib.metadata import PackageNotFoundError, version

# Derived from the nearest git tag by hatch-vcs (see pyproject.toml) at
# install/build time - no more manual version bumps. Package metadata is
# absent when running straight from a Docker image, which ships raw source
# rather than an installed package (see Dockerfile), so LNURL_MINT_VERSION
# is baked in there instead, from the same git tag, at image build time.
try:
    __version__ = version("lnurl-mint")
except PackageNotFoundError:
    __version__ = os.environ.get("LNURL_MINT_VERSION", "0.0.0+unknown")
