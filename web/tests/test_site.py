import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import Client

import opencs_web

SOURCE = Path(opencs_web.__file__).parent


def test_health_answers_ok() -> None:
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok\n"
    assert response["Content-Type"].startswith("text/plain")


def test_health_refuses_a_post() -> None:
    assert Client().post("/healthz").status_code == 405


def test_an_unknown_page_is_not_found() -> None:
    assert Client().get("/no-such-page").status_code == 404


def test_the_asgi_application_loads() -> None:
    from opencs_web.asgi import application  # noqa: PLC0415

    assert callable(application)


def test_the_settings_refuse_to_load_without_a_secret_key() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCS_WEB_")}
    env["DJANGO_SETTINGS_MODULE"] = "opencs_web.settings"
    result = subprocess.run(
        [sys.executable, "-m", "opencs_web", "check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "OPENCS_WEB_SECRET_KEY must be set" in result.stderr


@pytest.mark.parametrize("path", sorted(SOURCE.rglob("*.py")), ids=lambda p: p.name)
def test_the_web_never_imports_the_game_servers_store(path: Path) -> None:
    """The web reaches accounts only through the service channel (ADR 10, ADR 12)."""
    for node in ast.walk(ast.parse(path.read_text())):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not name.startswith(("opencs.store", "opencs.lobby")), f"{path.name}: {name}"
