"""Maps (ADR 16): recorded games and the live feed decoded into viewer tracks, seeking, the
audiences' delays, and the endpoints.

The fixtures are a 16-bot game: the first 50 seconds of its first segment (pilots joining, late
joiners sent the city) and the first 30 of its second (a rotation keyframe, then movement). Set
``OPENCS_RECORDINGS`` to a directory of ``.ocsrec`` files to also check every segment there, in
order: each late joiner's sync and each keyframe matches the track built so far, and each pod's
track is its reports."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from itertools import accumulate, pairwise
from pathlib import Path
from typing import Any

import pytest
from django.test import AsyncClient, override_settings

from fakeservice import FakeService, socket_dir
from opencs.messages import (
    client,
    client_payload,
    decode_client,
    decode_server,
    server,
    server_payload,
)
from opencs.messages.fields import WirePos
from opencs.recording import (
    Kind,
    encode_header,
    encode_record,
    keyframe_records,
    read_segment,
)
from opencs.wire.codec import BYTE_MIN
from opencs_web import mapviews
from opencs_web.maps import live as maplive
from opencs_web.maps.source import Header, Item, Recordings
from opencs_web.maps.track import CityTrack, Collected, encode, heading
from opencs_web.maps.windows import WINDOW_MS, build_window, window_start
from opencs_web.recordings.archive import Archive
from opencs_web.recordings.chunks import read_records, records_start

FIXTURES = Path(__file__).parent / "fixtures"
JOINS = FIXTURES / "B-20261111T193524Z-50s.ocsrec"
ROTATED = FIXTURES / "B-20261111T194024Z-30s.ocsrec"
FAR = 10**15  # "now", long after the fixtures


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    mapviews.reset()
    yield
    mapviews.reset()


def copy(source: Path, directory: Path, *, started: int | None = None) -> Path:
    """A fixture in ``directory``, its start moved to ``started`` (UTC ms) if given."""
    data = source.read_bytes()
    if started is not None:
        segment = read_segment(data)
        header = dict(segment.header, started_utc_ms=started)
        data = encode_header(header) + data[records_start(data) :]
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started / 1000))
        name = f"B-{stamp}.ocsrec"
    else:
        name = source.name.rsplit("-", 1)[0] + ".ocsrec"
    path = directory / name
    path.write_bytes(data)
    return path


def items(path: Path) -> list[Item]:
    segment = read_segment(path.read_bytes())
    t0 = int(segment.header["started_utc_ms"])
    return [Item(t0 + r.at_ms, r.kind, r.player, r.payload) for r in segment.records]


def columns(window: dict[str, Any], pod: int) -> list[tuple[int, ...]]:
    """A pod's reports in a window, the deltas summed: (t, x, z, h, s, r, f) with t in UTC ms."""
    cols = [list(accumulate(c)) for c in window["pods"][str(pod)]]
    return [(t + window["start"], *rest) for t, *rest in zip(*cols, strict=True)]


def reports_of(path: Path, pod: int) -> list[tuple[int, int, int, int]]:
    out = []
    for item in items(path):
        if item.kind is Kind.CLIENT and item.player == pod:
            m = decode_client(item.payload)
            if isinstance(m, client.PodState):
                out.append((item.at, m.pos.world_x, m.pos.world_z, heading(m.orient)))
    return out


def pods_of(state: dict[str, Any]) -> dict[int, tuple[int, int]]:
    return {p[0]: (p[1], p[2]) for p in state["pods"]}


# --- the track -----------------------------------------------------------------------------------


def test_a_pods_track_is_the_positions_it_reported(tmp_path: Path) -> None:
    path = copy(ROTATED, tmp_path)
    t0 = items(path)[0].at
    window = build_window(
        Recordings(None, tmp_path), "B", window_start(t0), window_start(t0) + WINDOW_MS, FAR
    )
    track = window.json(FAR)
    assert len(track["pods"]) == 16
    for pod in range(16):
        reported = reports_of(path, pod)
        rebuilt = [row[:4] for row in columns(track, pod) if row[0] < window.end]
        expected = [r for r in reported if r[0] < window.end]
        assert rebuilt == expected and expected


def test_a_keyframe_gives_the_city_a_late_joiner_is_sent(tmp_path: Path) -> None:
    path = copy(ROTATED, tmp_path)
    records = items(path)
    introduced = {
        m.id: (m.pos.world_x, m.pos.world_z)
        for m in (decode_server(i.payload)[0] for i in records if i.kind is Kind.SNAPSHOT)
        if isinstance(m, server.Introduce)
    }
    t0 = records[0].at
    window = build_window(
        Recordings(None, tmp_path), "B", window_start(t0), window_start(t0) + WINDOW_MS, FAR
    )
    assert window.state["pods"] == []  # before the recording: nothing known
    [key] = [e for e in window.collected.events if e[1] == "key"]
    assert key[0] == t0 and pods_of(key[2]) == introduced and len(introduced) == 16
    assert [p[1] for p in key[2]["pilots"]][:2] == ["BOT01", "BOT02"]
    assert {(n[1], n[2], n[3]) for n in key[2]["npcs"]} == {
        ("s", m.pos.world_x, m.pos.world_z)
        for m in (decode_server(i.payload)[0] for i in records if i.kind is Kind.SNAPSHOT)
        if isinstance(m, server.SpawnSentinel)
    }


def test_seeking_into_a_segment_gives_the_city_at_that_time(tmp_path: Path) -> None:
    path = copy(JOINS, tmp_path)
    records = items(path)
    at = window_start(records[-1].at)  # a whole minute inside the segment
    assert records[0].at < at < records[-1].at
    window = build_window(Recordings(None, tmp_path), "B", at, at + WINDOW_MS, FAR)
    last: dict[int, tuple[int, int]] = {}
    for item in records:
        if item.at >= at:
            break
        if item.kind is Kind.CLIENT and isinstance(
            m := decode_client(item.payload), client.PodState
        ):
            last[item.player or 0] = (m.pos.world_x, m.pos.world_z)
    assert pods_of(window.state) == last and len(last) >= 10
    assert {p[0] for p in window.state["pilots"]} >= set(last)


def check_syncs(track: CityTrack, records: list[Header | Item]) -> int:
    """Play ``records``; each late joiner's introduction of a pod (a ``'G'`` to that player
    alone) and each keyframe's must match the pod as the track has it. The number checked."""
    checked = 0
    for item in records:
        if isinstance(item, Header):
            track.begin_segment(item.header)
            continue
        if item.kind in (Kind.SERVER, Kind.SNAPSHOT):
            message = decode_server(item.payload)[0]
            if isinstance(message, server.Introduce) and (
                item.player is not None or item.kind is Kind.SNAPSHOT
            ):
                pod = track.pods.get(message.id)
                if pod is not None and item.kind is Kind.SERVER:
                    assert (pod.x, pod.z) == (message.pos.world_x, message.pos.world_z), item
                    checked += 1
        track.record(item.at, item.kind, item.player, item.payload)
    return checked


def test_what_late_joiners_are_sent_matches_the_track(tmp_path: Path) -> None:
    path = copy(JOINS, tmp_path)
    segment = read_segment(path.read_bytes())
    track = CityTrack("B")
    assert check_syncs(track, [Header(segment.header), *items(path)]) > 50
    assert track.undecodable == 0


def test_a_death_s_fly_over_camera_is_not_drawn_as_the_pod() -> None:
    t0 = 1_800_000_000_000
    pos = WirePos.from_world

    def b(x: int, z: int, state: int = 0) -> bytes:
        return client_payload(
            client.PodState(pos(x, 15, z), 900, 30, 30, 0, 0, state, 3000, 50, 100, 100)
        )

    header = {
        "city": "B",
        "started_utc_ms": t0,
        "config": {},
        "players": [{"id": 3, "callsign": "ALPHA", "team": 1}],
    }
    records = [
        encode_header(header),
        keyframe_records(0, "start", [], {"scores": []}),
        encode_record(100, Kind.CLIENT, 3, b(1000, 1000, 4)),
        encode_record(100, Kind.CLIENT, 3, b(1010, 1000)),
        encode_record(100, Kind.CLIENT, 3, client_payload(client.Died())),
        encode_record(1, Kind.SERVER, None, server_payload(server.KillNotice(True), BYTE_MIN)),
        encode_record(0, Kind.SERVER, None, server_payload(server.Destroy(3), BYTE_MIN)),
        encode_record(500, Kind.CLIENT, 3, b(4000, 4000, 4)),  # the camera, far off
        encode_record(500, Kind.CLIENT, 3, b(3900, 3900, 4)),
        encode_record(500, Kind.CLIENT, 3, client_payload(client.DropMe())),
        encode_record(100, Kind.CLIENT, 3, b(500, 500, 4)),  # dropped in again
    ]
    data = b"".join(records)
    segment = read_segment(data)
    track = CityTrack("B")
    track.begin_segment(segment.header)
    track.collecting = True
    for r in segment.records:
        track.record(t0 + r.at_ms, r.kind, r.player, r.payload)
    got = track.take()
    assert [(r[2], r[3]) for r in got.reports] == [(1000, 1000), (1010, 1000), (500, 500)]
    codes = [e[1] for e in got.events]
    assert codes == ["key", "live", "die", "live"]
    assert got.events[2][2:] == [3, None, 1]
    assert track.pods[3].alive


def test_without_its_drop_request_a_pod_stands_again_after_the_timeout() -> None:
    track = CityTrack("B")
    track.begin_segment({"players": [{"id": 1, "callsign": "A", "team": 0}]})
    report = client.PodState(WirePos.from_world(100, 15, 100), 0, 0, 0, 0, 0, 0, 3000, 50, 100, 100)
    track.record(0, Kind.CLIENT, 1, client_payload(report))
    track.record(10, Kind.SERVER, None, server_payload(server.Destroy(1), BYTE_MIN))
    far = client.PodState(WirePos.from_world(2000, 15, 2000), 0, 0, 0, 0, 0, 4, 3000, 50, 100, 100)
    track.record(19_000, Kind.CLIENT, 1, client_payload(far))
    assert (track.pods[1].x, track.pods[1].alive) == (100, False)
    track.record(20_100, Kind.CLIENT, 1, client_payload(far))
    assert (track.pods[1].x, track.pods[1].alive) == (2000, True)


def test_chat_is_only_in_a_track_that_asks_for_it() -> None:
    collected = Collected(events=[[5, "chat", 1, 0, "hello"], [6, "shot", 1, 0, 0, 0, 1]])
    public = encode("B", 0, 100, None, collected)
    assert [e[1] for e in public["ev"]] == ["shot"] and "hello" not in json.dumps(public)
    assert [e[1] for e in encode("B", 0, 100, None, collected, chat=True)["ev"]] == ["chat", "shot"]


def test_the_archive_gives_the_same_track_as_the_segment(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    path = copy(JOINS, raw)
    with Archive(tmp_path / "archive.db") as archive:
        assert archive.add_segment(path.name, path.read_bytes(), now_ms=0)
    t0 = items(path)[0].at
    for start in (window_start(t0), window_start(t0) + WINDOW_MS):
        from_file = build_window(Recordings(None, raw), "B", start, start + WINDOW_MS, FAR)
        from_archive = build_window(
            Recordings(tmp_path / "archive.db", None), "B", start, start + WINDOW_MS, FAR
        )
        assert from_archive.json(FAR) == from_file.json(FAR)
        assert from_archive.final and from_file.final


def test_spans_join_a_citys_segments(tmp_path: Path) -> None:
    copy(JOINS, tmp_path)
    copy(ROTATED, tmp_path)
    [(start, end), (later, last)] = Recordings(None, tmp_path).spans()["B"]
    assert start < end < later < last


def _corpus() -> list[Path]:
    extra = os.environ.get("OPENCS_RECORDINGS")
    found = sorted({p.parent for p in Path(extra).rglob("*.ocsrec")}) if extra else []
    return [FIXTURES, *found]


@pytest.mark.parametrize("directory", _corpus(), ids=str)
def test_every_recorded_game_in_the_corpus_replays(directory: Path) -> None:
    source = Recordings(None, directory)
    for city, spans in source.spans().items():
        for start, end in spans:
            records, _ = source.items(city, start, end + 1)
            track = CityTrack(city)
            assert check_syncs(track, records) > 0
            assert track.undecodable == 0
            window = build_window(
                source, city, window_start(start), window_start(start) + WINDOW_MS, FAR
            )
            assert window.json(FAR)["pods"]


# --- the endpoints and the delays ----------------------------------------------------------------


def body(response: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(response.content)
    return data


@pytest.fixture
def recent(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """The 50-second fixture as if it began 3615 seconds ago."""
    started = round(time.time() * 1000) - 3_615_000
    copy(JOINS, tmp_path, started=started)
    with override_settings(
        OPENCS_RECORDINGS=tmp_path,
        OPENCS_ARCHIVE=tmp_path / "none.db",
        OPENCS_MAP_DELAY_PUBLIC=3600,
        OPENCS_SERVICE_SOCKET=None,
    ):
        yield tmp_path, started


def test_the_public_never_gets_anything_newer_than_its_delay(recent: tuple[Path, int]) -> None:
    _, started = recent

    async def scenario() -> None:
        c = AsyncClient()
        listed = body(await c.get("/maps/cities.json"))
        assert listed["delay"] == 3600
        [city] = listed["cities"]
        assert city["city"] == "B" and city["live"] is False
        [[first, last]] = city["spans"]
        assert first == started
        assert last <= listed["limit"] <= listed["now"] - 3_600_000
        for at in (started, started + 30_000, started + 49_000):
            response = await c.get("/maps/B/track.json", {"t": at})
            if window_start(at) >= mapviews.limit_for(3_600_000):
                assert response.status_code == 403
                continue
            window = body(response)
            limit = mapviews.limit_for(3_600_000)
            assert window["end"] <= limit
            times = [e[0] + window["start"] for e in window["ev"]]
            times += [row[0] for pod in window["pods"] for row in columns(window, int(pod))]
            assert times and max(times) < window["end"]
        too_new = await c.get("/maps/B/track.json", {"t": round(time.time() * 1000) - 60_000})
        assert too_new.status_code == 403 and body(too_new)["error"] == "too-recent"
        assert (await c.get("/maps/B/track.json", {"t": "soon"})).status_code == 400
        assert (await c.get("/maps/BB/track.json", {"t": started})).status_code == 404

    asyncio.run(scenario())


def test_the_public_delay_of_0_is_a_config_change(recent: tuple[Path, int]) -> None:
    _, started = recent
    with override_settings(OPENCS_MAP_DELAY_PUBLIC=0):

        async def scenario() -> dict[str, Any]:
            return body(await AsyncClient().get("/maps/B/track.json", {"t": started + 49_000}))

        window = asyncio.run(scenario())
    assert window["end"] == window["start"] + WINDOW_MS


def test_the_map_is_the_citys_blocks(recent: tuple[Path, int]) -> None:
    async def scenario() -> tuple[int, dict[str, Any], int]:
        c = AsyncClient()
        found = await c.get("/maps/B/map.json")
        missing = await c.get("/maps/Q/map.json")
        return found.status_code, body(found), missing.status_code

    status, city, missing = asyncio.run(scenario())
    assert (status, missing) == (200, 404)
    assert city["city"] == "B" and city["size"] == 4800 and city["block"] == 600
    assert city["teams"] == ["RED", "BLUE", "GREEN", "BLACK"]
    assert city["blocks"] is None or len(city["blocks"]) == 8


def test_the_delayed_stream_plays_the_recording_behind_the_delay(recent: tuple[Path, int]) -> None:
    async def scenario() -> list[dict[str, Any]]:
        out = []
        stream = mapviews.track_events("B", 3_600_000, False, "10.0.0.1", lifetime=2.5)
        async for chunk in stream:
            if chunk.startswith(b"event: track"):
                out.append(json.loads(chunk.split(b"data: ", 1)[1]))
        return out

    windows = asyncio.run(scenario())
    limit = mapviews.limit_for(3_600_000)
    assert windows[0]["state"] is not None and all(w["state"] is None for w in windows[1:])
    assert len(windows) >= 2
    for before, after in pairwise(windows):
        assert after["start"] == before["end"]
    assert all(w["end"] <= limit for w in windows)
    assert sum(len(w["pods"]) for w in windows) > 0


def test_map_streams_are_capped(recent: tuple[Path, int]) -> None:
    with override_settings(OPENCS_MAP_MAX_STREAMS=1):

        async def scenario() -> bytes:
            first = mapviews.track_events("B", 3_600_000, False, "10.0.0.1", lifetime=1.5)
            await anext(first)
            second = mapviews.track_events("B", 3_600_000, False, "10.0.0.2", lifetime=1.5)
            refused = await anext(second)
            await first.aclose()
            return refused

        assert b"event: busy" in asyncio.run(scenario())


def test_requests_are_rate_limited(recent: tuple[Path, int]) -> None:
    async def scenario() -> list[int]:
        c = AsyncClient()
        return [(await c.get("/maps/cities.json")).status_code for _ in range(70)]

    codes = asyncio.run(scenario())
    assert codes[0] == 200 and codes[-1] == 429


# --- the live feed -------------------------------------------------------------------------------


def feed_lines(path: Path, letter: str = "B", per_line: int = 200) -> list[dict[str, Any]]:
    """A segment cut into feed lines as the server sends them: the header and keyframe, then the
    records."""
    data = path.read_bytes()
    records = list(read_records(data, 0, records_start(data)))
    end = next(i for i, r in enumerate(records) if r.kind is Kind.KEYFRAME_END)

    def line(raw: bytes, **more: Any) -> dict[str, Any]:
        return {"feed": letter, "epoch": 1, "data": base64.b64encode(raw).decode(), **more}

    out = [line(data[: records[end].end], start="subscribe", dropped=0)]
    rest = records[end + 1 :]
    for i in range(0, len(rest), per_line):
        part = rest[i : i + per_line]
        out.append(line(data[part[0].start : part[-1].end]))
    return out


def test_a_live_city_decodes_its_feed_into_pieces_for_every_viewer() -> None:
    async def scenario() -> None:
        city = maplive.LiveCity("B")
        lines = feed_lines(ROTATED)
        city.line(lines[0])
        joining = asyncio.create_task(city.join())
        await asyncio.sleep(0)
        city.tick()
        seq, state = await joining
        window = json.loads(state)
        assert len(window["state"]["pods"]) == 16 and window["start"] == window["end"]
        for line in lines[1:]:
            city.line(line)
        city.tick()
        [piece] = city.after(seq) or []
        decoded = json.loads(city.piece_bytes(piece, False))
        assert decoded["state"] is None and len(decoded["pods"]) == 16
        assert city.piece_bytes(piece, False) is city.piece_bytes(piece, False)  # encoded once
        for _ in range(maplive.PIECES_KEPT + 1):
            city.tick()
        assert city.after(seq) is None  # too far behind: join again
        city.line({"feed": "B", "end": "lost"})
        assert not city.online and city.track.pods == {}

    asyncio.run(scenario())


@pytest.fixture
def service() -> Iterator[Path]:
    with socket_dir() as directory:
        yield directory / "service.sock"


async def started(path: Path) -> FakeService:
    fake = FakeService(path)
    fake.feeds["B"] = feed_lines(ROTATED)
    return await fake.start()


def test_live_viewers_share_one_feed_that_stops_with_the_last(service: Path) -> None:
    async def scenario() -> None:
        fake = await started(service)
        try:
            with override_settings(OPENCS_SERVICE_SOCKET=service):
                hub = mapviews.hub()

                async def watch(n: int) -> list[dict[str, Any]]:
                    got: list[dict[str, Any]] = []
                    stream = mapviews.track_events("B", 0, False, f"10.0.0.{n}", lifetime=2.5)
                    async for chunk in stream:
                        if chunk.startswith(b"event: track"):
                            got.append(json.loads(chunk.split(b"data: ", 1)[1]))
                    return got

                first, second = await asyncio.gather(watch(1), watch(2))
                for got in (first, second):
                    assert got[0]["state"] is not None and len(got[0]["state"]["pods"]) == 16
                subscribes = [r for r in fake.requests if r["op"] == "subscribe"]
                assert len(subscribes) == 1 and fake.connections == 1
                for _ in range(50):
                    if any(r["op"] == "unsubscribe" for r in fake.requests):
                        break
                    await asyncio.sleep(0.02)
                assert [r["op"] for r in fake.requests][-1] == "unsubscribe"
                assert hub.cities == {}
        finally:
            await mapviews.hub().client.close()
            await fake.stop()

    asyncio.run(scenario())


async def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]
