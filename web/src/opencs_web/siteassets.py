"""The site's own style sheet and scripts, served from the package (no external assets), and the
version their addresses carry."""

import hashlib
from functools import lru_cache
from importlib.resources import files

#: The page's own style sheet and script, served from the package: no external assets.
ASSETS = {
    "site.css": "text/css; charset=utf-8",
    "live.js": "text/javascript; charset=utf-8",
    "maps.js": "text/javascript; charset=utf-8",
    "maptrack.js": "text/javascript; charset=utf-8",
}


@lru_cache
def asset_bytes(name: str) -> bytes:
    return files("opencs_web").joinpath("assets", name).read_bytes()


@lru_cache
def asset_version() -> str:
    """A short hash of every asset: changes whenever any of them does."""
    digest = hashlib.sha256()
    for name in sorted(ASSETS):
        digest.update(name.encode() + b"\0" + asset_bytes(name) + b"\0")
    return digest.hexdigest()[:12]
