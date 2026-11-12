"""The map viewer: its pages (the public's and the administrators'), what each gives the script,
its assets, and the script's track logic, run with node when node is installed (skipped if not)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.test import Client
from pytest_django.fixtures import Settings as SettingsWrapper

from opencs_web import mapviews
from opencs_web.maps.source import Recordings
from opencs_web.maps.windows import WINDOW_MS, build_window, window_start
from test_staff import ThreadedService, log_in, make_admin, service  # noqa: F401

pytestmark = pytest.mark.django_db

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures"


def text(response: object) -> str:
    return str(response.content.decode())  # type: ignore[attr-defined]


def viewer_data(page: str) -> dict[str, str]:
    [tag] = re.findall(r'<div id="viewer"[^>]*>', page)
    return dict(re.findall(r'data-([a-z-]+)="([^"]*)"', tag))


def assert_strict(response: object, page: str) -> None:
    csp = response["Content-Security-Policy"]  # type: ignore[index]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "connect-src 'self'" in csp and "unsafe" not in csp
    assert "<style" not in page and " style=" not in page and "<script>" not in page
    assert re.findall(r"<script[^>]*>", page) == ['<script type="module" src="/assets/maps.js">']
    assert "http" not in page  # nothing from elsewhere


def test_delays_are_said_plainly() -> None:
    words = mapviews.delay_words
    assert words(3600) == "one hour"
    assert words(60) == "one minute"
    assert words(5400) == "one hour and 30 minutes"
    assert words(7322) == "2 hours, 2 minutes and 2 seconds"
    assert words(45) == "45 seconds"
    assert words(0) == ""


def test_the_public_maps_page_gives_the_public_delay_and_never_chat(
    settings: SettingsWrapper,
) -> None:
    settings.OPENCS_MAP_DELAY_PUBLIC = 3600
    settings.OPENCS_MAP_ADMIN_CHAT = True  # for administrators only, whatever it says
    response = Client().get("/maps")
    page = text(response)
    assert response.status_code == 200
    assert_strict(response, page)
    data = viewer_data(page)
    assert data == {
        "base": "/maps/",
        "delay": "3600",
        "delay-words": "one hour",
        "chat": "0",
        "admin": "0",
    }
    assert "shown one hour behind" in page
    assert "map-chat" not in page and "Chat" not in page
    # without JavaScript: a notice, and the viewer stays hidden
    assert "<noscript>" in page and "The map needs JavaScript" in page
    assert re.search(r'<div id="viewer" class="viewer" hidden', page)
    # accessible controls: labelled buttons and inputs
    for control in ("map-play", "map-back", "map-forward", "map-live", "map-zoom-in"):
        assert re.search(rf'<button type="button" id="{control}"', page)
    assert '<label for="map-speed">' in page and '<label for="map-city">' in page
    assert 'id="map-seek"' in page and 'aria-label="Time"' in page


def test_with_no_public_delay_the_page_says_so(settings: SettingsWrapper) -> None:
    settings.OPENCS_MAP_DELAY_PUBLIC = 0
    page = text(Client().get("/maps"))
    assert viewer_data(page)["delay"] == "0"
    assert "shown as they are played" in page


def test_the_status_page_links_the_maps(settings: SettingsWrapper) -> None:
    settings.OPENCS_SERVICE_SOCKET = None
    settings.OPENCS_MAP_DELAY_PUBLIC = 3600
    page = text(Client().get("/"))
    assert '<a href="/maps">Maps</a>' in page
    assert '<a href="/maps">Maps of the cities</a>, shown one hour behind' in page


def test_the_viewer_s_scripts_are_served_and_cached() -> None:
    client = Client()
    for name in ("maps.js", "maptrack.js"):
        response = client.get(f"/assets/{name}")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/javascript; charset=utf-8"
        assert "max-age" in response["Cache-Control"]
    script = text(client.get("/assets/maps.js"))
    # it imports only its own module, and never writes markup
    assert re.findall(r'from "([^"]+)"', script) == ["./maptrack.js"]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "document.write" not in script


def test_a_city_s_map_may_be_kept_a_while(tmp_path: Path, settings: SettingsWrapper) -> None:
    shutil.copy(FIXTURES / "B-20261111T193524Z-50s.ocsrec", tmp_path / "B-20261111T193524Z.ocsrec")
    settings.OPENCS_RECORDINGS = tmp_path
    settings.OPENCS_ARCHIVE = tmp_path / "none.db"
    settings.OPENCS_SERVICE_SOCKET = None
    mapviews.reset()
    try:
        response = Client().get("/maps/B/map.json")
    finally:
        mapviews.reset()
    assert response.status_code == 200
    assert response["Cache-Control"] == f"max-age={mapviews.MAP_MAX_AGE}"


# --- the administrators' page --------------------------------------------------------------------


def test_the_admin_maps_page_is_for_admins_logged_in(
    service: ThreadedService,  # noqa: F811
    settings: SettingsWrapper,
) -> None:
    assert Client().get("/admin/maps")["Location"] == "/admin/login"
    settings.OPENCS_MAP_DELAY_ADMIN = 0
    settings.OPENCS_MAP_DELAY_PUBLIC = 3600
    client = Client()
    log_in(client, make_admin())
    response = client.get("/admin/maps")
    page = text(response)
    assert response.status_code == 200
    assert_strict(response, page)
    assert response["Cache-Control"] == "no-store"
    data = viewer_data(page)
    assert (data["delay"], data["chat"], data["admin"]) == ("0", "0", "1")
    assert "Shown as the games are played" in page and "Without chat" in page
    assert "The public's maps are one hour behind" in page
    assert "map-chat" not in page
    # linked from the admin status page and the admin pages' menu
    home = text(client.get("/admin/"))
    assert '<a href="/admin/maps">Maps of the cities</a>, as they are played' in home


def test_the_admin_maps_page_has_chat_only_when_the_setting_allows(
    service: ThreadedService,  # noqa: F811
    settings: SettingsWrapper,
) -> None:
    settings.OPENCS_MAP_ADMIN_CHAT = True
    settings.OPENCS_MAP_DELAY_ADMIN = 30
    client = Client()
    log_in(client, make_admin())
    page = text(client.get("/admin/maps"))
    data = viewer_data(page)
    assert (data["delay"], data["chat"]) == ("30", "1")
    assert 'id="map-chat"' in page and "With the recorded chat" in page


def test_the_admin_maps_page_is_not_there_when_the_admin_pages_are_off(
    settings: SettingsWrapper,
) -> None:
    settings.OPENCS_ADMIN = False
    assert Client().get("/admin/maps").status_code == 404


# --- the script's logic, with node ---------------------------------------------------------------


def test_the_viewer_s_track_logic(tmp_path: Path) -> None:
    """``web/tests/js/maptrack.test.mjs`` against each minute of the fixtures, as served."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    recordings = tmp_path / "rec"
    recordings.mkdir()
    for fixture in FIXTURES.glob("*.ocsrec"):
        shutil.copy(fixture, recordings / (fixture.name.rsplit("-", 1)[0] + ".ocsrec"))
    source = Recordings(tmp_path / "none.db", recordings)
    windows = tmp_path / "windows"
    windows.mkdir()
    for city, spans in source.spans().items():
        for first, last in spans:
            start = window_start(first)
            while start < last:
                window = build_window(source, city, start, start + WINDOW_MS, 10**15)
                body = window.json(start + WINDOW_MS)
                (windows / f"{city}-{start}.json").write_text(json.dumps(body))
                start += WINDOW_MS
    scripts = sorted(str(p) for p in (TESTS / "js").glob("*.test.mjs"))
    assert scripts
    result = subprocess.run(
        [node, "--test", *scripts],  # files: Node 24 does not search a directory
        env={"OPENCS_MAP_WINDOWS": str(windows), "PATH": str(Path(node).parent)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout or "fail 0" in result.stdout
