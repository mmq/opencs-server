"""Layering guard (DESIGN §3): a layer imports only the layers above it in the table, and the root
modules too: nothing below ``cities``, ``control``, ``admin`` and ``app`` imports them."""

from __future__ import annotations

import ast
from pathlib import Path

import opencs

ROOT = Path(opencs.__file__).parent

#: package -> the ``opencs`` modules/packages it may import (besides itself).
ALLOWED: dict[str, set[str]] = {
    "wire": {"policy"},
    "messages": {"wire", "policy"},
    "store": {"policy"},
    "lobby": {"wire", "policy", "config", "store"},
    "session": {
        "wire",
        "messages",
        "lobby",
        "store",
        "policy",
        "config",
        "clock",
        "connlog",
        "ratelimit",
        "world",
        "routing",
    },
    "net": {"wire", "messages", "policy", "clock", "connlog", "ratelimit"},
    "world": {"wire", "messages", "policy", "config"},
    "routing": {"messages", "world", "policy"},
    # The shared root modules: leaves any layer may use.
    "policy": set(),
    "clock": set(),
    "connlog": set(),
    "tasks": set(),
    "ratelimit": {"policy"},
    "config": {"messages", "policy"},
    "logsetup": {"config"},
    # The root modules above the sessions: the city tickers, the operator's commands and the admin
    # channel, then the app that builds everything.
    "cities": {"clock", "config", "policy", "routing", "session", "tasks", "world"},
    "control": {
        "cities",
        "config",
        "connlog",
        "lobby",
        "messages",
        "policy",
        "routing",
        "session",
        "tasks",
        "world",
    },
    "admin": {"clock", "config", "control", "policy", "ratelimit"},
    "app": {
        "__version__",
        "admin",
        "cities",
        "clock",
        "config",
        "connlog",
        "control",
        "lobby",
        "logsetup",
        "net",
        "policy",
        "routing",
        "session",
        "store",
        "tasks",
        "world",
    },
    "__main__": {"app"},
    "__init__": set(),
}

#: The modules only the app (and, for ``cities``/``control``/``admin``, one another) may import.
ABOVE_THE_SESSIONS = {"cities", "control", "admin", "app"}


#: Packages that must be sans-IO (DESIGN §3, §3.4) and the standard-library modules they may not
#: import: no sockets, no event loop, no threads, no wall clock.
SANS_IO = ("session", "lobby", "world", "routing")
FORBIDDEN_STDLIB = {"asyncio", "socket", "selectors", "threading", "time", "select", "ssl"}


def _sources(package: str) -> list[Path]:
    """The modules of a package, or the module itself for ``routing.py``."""
    folder = ROOT / package
    return sorted(folder.rglob("*.py")) if folder.is_dir() else [folder.with_suffix(".py")]


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise AssertionError(f"{path}: use absolute imports")
            base = node.module or ""
            found.add(base)
            found |= {f"{base}.{a.name}" for a in node.names}
    return found


def test_packages_import_only_allowed_layers() -> None:
    for package, allowed in ALLOWED.items():
        for path in _sources(package):
            for name in _imports(path):
                parts = name.split(".")
                if parts[0] != "opencs" or len(parts) == 1:
                    continue
                top = parts[1]
                assert top == package or top in allowed, f"{path.name} imports {name}"


def test_the_guard_covers_the_layers_the_design_forbids() -> None:
    # net knows nothing of session, lobby or world; session nothing of net; lobby of neither.
    assert not {"session", "lobby", "world"} & ALLOWED["net"]
    assert "net" not in ALLOWED["session"]
    assert not {"net", "session"} & ALLOWED["lobby"]
    assert not {"net", "session", "lobby", "routing", "asyncio"} & ALLOWED["world"]
    assert not {"net", "session", "lobby", "world"} & ALLOWED["wire"]
    assert not {"net", "session", "lobby", "world"} & ALLOWED["messages"]
    # Nothing at or below the sessions reaches up to the root modules above them.
    for package in ("wire", "messages", "net", "store", "world", "lobby", "routing", "session"):
        assert not ABOVE_THE_SESSIONS & ALLOWED[package], package
    assert not {"control", "admin", "app"} & ALLOWED["cities"]
    assert not {"admin", "app"} & ALLOWED["control"]
    assert "app" not in ALLOWED["admin"]


def test_every_module_and_package_has_a_layer() -> None:
    """A new top-level module or package must be given its allowed imports here, and so a place in
    the design."""
    found = {
        entry.name if entry.is_dir() else entry.stem
        for entry in ROOT.iterdir()
        if (entry.is_dir() and (entry / "__init__.py").exists()) or entry.suffix == ".py"
    }
    assert found == set(ALLOWED)


def test_sans_io_packages_import_no_io_modules() -> None:
    for package in SANS_IO:
        for path in _sources(package):
            for name in _imports(path):
                assert name.split(".")[0] not in FORBIDDEN_STDLIB, f"{path.name} imports {name}"
