import asyncio
import copy
import html
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.test import AsyncClient, Client, override_settings

from fakeservice import SNAPSHOT, FakeService, socket_dir
from opencs_web import live
from opencs_web.siteassets import asset_version
from opencs_web.status import StatusCache

#: Callsigns are 3 to 12 of ``!``..``~``: every one of these is a legal callsign.
HOSTILE = ["<script>", "&amp;", '"onx="1', "'x'", "</ul>", "<b>BOLD</b>", "A&B<C>D"]


@pytest.fixture(autouse=True)
def fresh_live() -> Iterator[None]:
    live.reset()
    yield
    live.reset()


def use_snapshot(snapshot: object) -> None:
    async def fetch() -> object:
        return snapshot

    live._cache = StatusCache(fetch)


def hostile_snapshot() -> dict[str, Any]:
    snapshot = copy.deepcopy(SNAPSHOT)
    snapshot["cities"][0]["name"] = "<i>CITY</i>"
    snapshot["cities"][0]["players"] = [
        {"callsign": c, "team": "RED", "guest": i % 2 == 0} for i, c in enumerate(HOSTILE)
    ]
    return snapshot


def test_the_home_page_shows_the_status() -> None:
    use_snapshot(SNAPSHOT)
    response = Client().get("/")
    assert response.status_code == 200
    page = response.content.decode()
    assert "Game server: online" in page
    assert "<dt>Accounts</dt><dd>12</dd>" in page
    assert "City B: BOSTON" in page
    assert "2 of 16 pilots" in page
    assert "<dd>1 hour, 2 minutes</dd>" in page
    assert re.search(r"<li>ALPHA</li>", page)
    assert re.search(r'<li>BOT01 <span class="guest">\(guest\)</span></li>', page)
    # What the snapshot has and the public page does not show.
    assert "registration" not in page and "chamber" not in page.lower()


def test_every_callsign_and_name_is_escaped() -> None:
    use_snapshot(hostile_snapshot())
    page = Client().get("/").content.decode()
    for callsign in HOSTILE:
        assert f"<li>{html.escape(callsign)}" in page
    assert "<script>" not in page.replace(
        f'<script src="/assets/{asset_version()}/live.js" defer></script>', ""
    )
    assert "<b>" not in page and "<i>" not in page and "</ul></li>" not in page
    assert "&lt;i&gt;CITY&lt;/i&gt;" in page


def test_the_live_block_is_escaped_too() -> None:
    from opencs_web.views import render_status  # noqa: PLC0415

    use_snapshot(hostile_snapshot())
    status = asyncio.run(live.status_cache().get())
    block = render_status(status)
    for callsign in HOSTILE:
        assert f"<li>{html.escape(callsign)}" in block
    assert "<script>" not in block and "<b>" not in block


def test_the_page_carries_a_strict_content_security_policy() -> None:
    use_snapshot(SNAPSHOT)
    response = Client().get("/")
    csp = response["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe" not in csp
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    page = response.content.decode()
    assert "<style" not in page and " style=" not in page and "<script>" not in page
    assert "http" not in page  # nothing from elsewhere


@override_settings(OPENCS_SERVICE_SOCKET=Path("/nonexistent/service.sock"))
def test_with_no_socket_the_page_says_offline() -> None:
    response = Client().get("/")
    assert response.status_code == 200
    assert "Game server: offline" in response.content.decode()


@override_settings(OPENCS_SERVICE_SOCKET=None)
def test_with_no_socket_configured_the_page_says_offline() -> None:
    response = Client().get("/")
    assert response.status_code == 200
    assert "Game server: offline" in response.content.decode()


def test_a_refusing_server_shows_offline_and_a_working_one_online() -> None:
    with socket_dir() as d:
        sock = d / "service.sock"

        async def scenario() -> list[str]:
            pages = []
            fake = await FakeService(sock).start()
            with override_settings(OPENCS_SERVICE_SOCKET=sock):
                client = AsyncClient()
                fake.hello_reply = {"ok": False, "error": "version", "message": "no"}
                pages.append((await client.get("/")).content.decode())
                live.reset()
                fake.hello_reply = None
                pages.append((await client.get("/")).content.decode())
                live.reset()
                fake.refuse = "rate"
                pages.append((await client.get("/")).content.decode())
            await fake.stop()
            return pages

        refused, working, rate = asyncio.run(scenario())
    assert "Game server: offline" in refused
    assert "Game server: online" in working
    assert "Game server: offline" in rate


def test_the_event_stream_sends_the_status_block() -> None:
    use_snapshot(hostile_snapshot())

    async def scenario() -> tuple[int, str, list[bytes]]:
        response = await AsyncClient().get("/status/events")
        stream = aiter(response.streaming_content)  # type: ignore[attr-defined]
        chunks = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return response.status_code, response["Content-Type"], chunks

    status, content_type, chunks = asyncio.run(scenario())
    assert status == 200
    assert content_type == "text/event-stream"
    assert chunks[0].startswith(b"retry: ")
    text = chunks[1].decode()
    assert text.startswith("event: status\n")
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert live.streams().open == 0


@override_settings(OPENCS_SERVICE_SOCKET=None)
def test_the_event_stream_sends_offline() -> None:
    async def scenario() -> bytes:
        response = await AsyncClient().get("/status/events")
        stream = aiter(response.streaming_content)  # type: ignore[attr-defined]
        await anext(stream)
        chunk: bytes = await anext(stream)
        await stream.aclose()
        return chunk

    assert b"Game server: offline" in asyncio.run(scenario())


def test_the_assets_are_served_and_nothing_else() -> None:
    client = Client()
    version = asset_version()
    css = client.get(f"/assets/{version}/site.css")
    assert css.status_code == 200 and css["Content-Type"].startswith("text/css")
    assert f'href="/assets/{version}/site.css"' in client.get("/").content.decode()
    js = client.get(f"/assets/{version}/live.js")
    assert js.status_code == 200 and b"EventSource" in js.content
    assert client.get(f"/assets/{version}/settings.py").status_code == 404
    assert client.get(f"/assets/{version}/..%2Fviews.py").status_code == 404
    assert client.get("/assets/live.js").status_code == 404


def test_a_page_from_before_an_upgrade_gets_the_current_asset_uncached() -> None:
    response = Client().get("/assets/000000000000/live.js")
    assert response.status_code == 200 and b"EventSource" in response.content
    assert response["Cache-Control"] == "no-cache"


def test_the_deploy_checks_leave_only_the_hsts_choices() -> None:
    """``check --deploy`` with production settings: what remains is explained in OPERATING."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCS_WEB_")}
    env.update(
        DJANGO_SETTINGS_MODULE="opencs_web.settings",
        OPENCS_WEB_SECRET_KEY="k" * 20 + "".join(chr(ord("a") + i) for i in range(26)) + "0123",
        OPENCS_WEB_ALLOWED_HOSTS="opencs.example.org",
        OPENCS_WEB_DATABASE=":memory:",
    )
    result = subprocess.run(
        [sys.executable, "-m", "opencs_web", "check", "--deploy"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    found = set(re.findall(r"\(security\.(W\d+)\)", result.stdout + result.stderr))
    assert found == {"W005", "W021"}, result.stdout + result.stderr
