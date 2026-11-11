"""The server's runtime dependencies (ADR 1, amended by ADR 13): `argon2-cffi` and what it pulls in,
nothing else. The web package (web/) is a workspace member with dependencies of its own; it is in
the dev group only, so `uv sync --no-dev` must still install the server alone."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
#: argon2-cffi, its compiled bindings and the bindings' cffi.
RUNTIME = {"opencs", "argon2-cffi", "argon2-cffi-bindings", "cffi", "pycparser"}


def _lock() -> dict[str, dict[str, Any]]:
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    return {p["name"]: p for p in packages}


def test_the_server_declares_argon2_cffi_alone() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert [d.split(">")[0].split("=")[0] for d in project["dependencies"]] == ["argon2-cffi"]
    assert "optional-dependencies" not in project


def test_a_sync_without_dev_installs_the_server_and_argon2_alone() -> None:
    """What `uv sync --no-dev` installs: the lock's runtime closure of `opencs`, no dev group."""
    lock = _lock()
    seen: set[str] = set()
    todo = ["opencs"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo += [d["name"] for d in lock[name].get("dependencies", [])]
    assert seen == RUNTIME


def test_the_web_package_is_only_a_dev_dependency_of_the_server() -> None:
    server = _lock()["opencs"]
    assert "opencs-web" not in {d["name"] for d in server.get("dependencies", [])}
    assert "opencs-web" in {d["name"] for d in server["dev-dependencies"]["dev"]}
