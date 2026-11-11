"""A lossless transform of a chunk's records, so they compress better: ``decode(encode(c)) == c``,
byte for byte, for any bytes ``c``.

Most of a recording is the clients' pod state reports (``'b'``, 96% of the bytes in a busy city).
The transform splits the records into streams by field (kinds, sources, times, lengths, other
payloads, and one stream per pod-state field), so like values sit together. With ``predict`` (the
default) each pod-state field is stored as its difference from what the same pilot's previous
report in the chunk predicts (``_report``):

- position x and z: dead reckoning, at the mean of the two reports' speed (``fwd``, feet a second)
  along the mean of their headings (``orient``, tenths of a degree);
- heading: the previous one turned at the mean of the two turn rates (``v1``, tenths of a degree
  a second), unless either report is a jump (``state`` 1, when ``v1`` is a vertical speed);
- ``v2``: this report's ``v1``;
- every other field: the previous report's value.

Differences are zigzag varints. A report is only coded this way when decoding it and encoding it
again gives back its exact bytes; anything else (another message, a non-canonical encoding, an
undecodable payload) is kept as it is. Bytes that are not whole canonical records are kept whole
(mode 0), and so is a chunk the streams would make larger. The prediction starts afresh in
every chunk, so a chunk decodes on its own.

The arithmetic is integer only, with a sine table built once; ``SINE_TABLE_CRC`` pins the table, so
a platform that built it differently is caught by the tests instead of decoding wrongly.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable

from opencs.messages import DecodeError, client_payload, decode_client
from opencs.messages.client import PodState
from opencs.messages.fields import WirePos
from opencs.recording import Kind, varint
from opencs_web.recordings.chunks import read_records, read_varint

#: The first byte of a transformed chunk.
MODE_VERBATIM = 0  # the bytes as given
MODE_COLUMNS = 1  # split into streams, values as they are
MODE_PREDICT = 2  # split into streams, pod-state values as differences from a prediction

_CODED = 0x10  # in a tag byte: this record is a coded pod state report

#: Tenths of a degree in a turn, and the sine table's scale.
TURN = 3600
SCALE = 1 << 14
SINE = tuple(round(math.sin(i * math.tau / TURN) * SCALE) for i in range(TURN))
SINE_TABLE_CRC = 0x16AB9841

# The pod-state fields, in stream order (the order of ``PodState``'s fields).
_FIELDS = (
    "x",
    "y",
    "z",
    "orient",
    "fwd",
    "fwd_target",
    "v1",
    "v2",
    "state",
    "power",
    "weight",
    "armor0",
    "armor1",
)
_X, _Y, _Z, _ORIENT, _FWD, _FWD_TARGET, _V1, _V2, _STATE = range(9)
_UNCHANGED = (_STATE, _V1, 9, 10, 11, 12, _FWD, _FWD_TARGET, _Y)  # guessed as the previous value
_NONE = (0,) * len(_FIELDS)
_VERTICAL = 1  # a ``state`` in which v1 and v2 are vertical speeds, not turn rates
_STREAMS = 5 + len(_FIELDS)  # tags, sources, times, lengths, payloads, fields


class TransformError(ValueError):
    """Bytes that are not a transformed chunk."""


def _zigzag(n: int) -> int:
    return n << 1 if n >= 0 else (-n << 1) - 1


def _unzigzag(n: int) -> int:
    return n >> 1 if not n & 1 else -((n + 1) >> 1)


def _round_div(n: int, d: int) -> int:
    """``n / d`` rounded half up, for ``d > 0``."""
    return (2 * n + d) // (2 * d)


def _sin(orient: int) -> int:
    return SINE[orient % TURN]


def _cos(orient: int) -> int:
    return SINE[(orient + TURN // 4) % TURN]


def _report(
    previous: tuple[int, ...] | None, dt: int, value: Callable[[int, int], int]
) -> tuple[int, ...]:
    """A report's fields, worked out field by field from the guesses: ``value(i, guess)`` gives
    field ``i``'s value (the encoder knows it and stores ``value - guess``; the decoder reads the
    difference and adds it). ``previous`` is the pilot's last report, ``dt`` the ms since."""
    p = previous or _NONE
    v = [0] * len(_FIELDS)
    for i in _UNCHANGED:
        v[i] = value(i, p[i])
    v[_V2] = value(_V2, v[_V1])
    turn = dx = dz = 0
    if previous is not None and _VERTICAL not in (p[_STATE], v[_STATE]):
        turn = _round_div((p[_V1] + v[_V1]) * dt, 2 * 1000)
    v[_ORIENT] = value(_ORIENT, p[_ORIENT] + turn)
    if previous is not None:
        speed = p[_FWD] + v[_FWD]  # twice the mean
        sin = _sin(p[_ORIENT]) + _sin(v[_ORIENT])  # twice the mean, times SCALE
        cos = _cos(p[_ORIENT]) + _cos(v[_ORIENT])
        dx = _round_div(speed * dt * sin, 4 * 1000 * SCALE)
        dz = _round_div(speed * dt * cos, 4 * 1000 * SCALE)
    v[_X] = value(_X, p[_X] + dx)
    v[_Z] = value(_Z, p[_Z] + dz)
    return tuple(v)


def _pod_values(kind: Kind, payload: bytes) -> tuple[int, ...] | None:
    """A pod state report's fields, if it is one that encodes back to exactly ``payload``."""
    if kind is not Kind.CLIENT or payload[:1] != b"b":
        return None
    try:
        msg = decode_client(payload)
    except DecodeError:
        return None
    if not isinstance(msg, PodState) or client_payload(msg) != payload:
        return None
    pos = msg.pos
    x, z = pos.world_x, pos.world_z
    try:
        if WirePos.from_world(x, pos.y, z) != pos:
            return None
    except ValueError:
        return None
    rest = (
        msg.fwd_target,
        msg.v1,
        msg.v2,
        msg.state,
        msg.power,
        msg.weight,
        msg.armor0,
        msg.armor1,
    )
    return (x, pos.y, z, msg.orient, msg.fwd, *rest)


def _pod_payload(v: tuple[int, ...]) -> bytes:
    return client_payload(PodState(WirePos.from_world(v[_X], v[_Y], v[_Z]), *v[_ORIENT:]))


def _storer(fields: list[bytearray], values: tuple[int, ...]) -> Callable[[int, int], int]:
    """``_report``'s ``value`` for the encoder: stores each field's difference from its guess."""

    def store(i: int, guess: int) -> int:
        fields[i] += varint(_zigzag(values[i] - guess))
        return values[i]

    return store


def encode(chunk: bytes, predict: bool = True) -> bytes:
    """``chunk`` (records) transformed; see the module's description."""
    tags, sources, times, lengths, payloads = (bytearray() for _ in range(5))
    fields = [bytearray() for _ in _FIELDS]
    last: dict[int, tuple[int, tuple[int, ...]]] = {}  # source: time and fields of its last report
    end = count = 0
    for record in read_records(chunk):
        head = varint(record.delta_ms) + bytes([record.kind]) + varint(record.source)
        if chunk[record.start : record.end] != head + varint(len(record.payload)) + record.payload:
            return bytes([MODE_VERBATIM]) + chunk  # not canonical: keep it whole
        values = _pod_values(record.kind, record.payload)
        sources += varint(record.source)
        times += varint(record.delta_ms)
        if values is None:
            tags.append(record.kind)
            lengths += varint(len(record.payload))
            payloads += record.payload
        elif predict:
            tags.append(record.kind | _CODED)
            at, previous = last.get(record.source, (record.at_ms, None))
            _report(previous, record.at_ms - at, _storer(fields, values))
            last[record.source] = (record.at_ms, values)
        else:
            tags.append(record.kind | _CODED)
            for i, value in enumerate(values):
                fields[i] += varint(_zigzag(value))
        end = record.end
        count += 1
    if end != len(chunk):
        return bytes([MODE_VERBATIM]) + chunk  # trailing bytes that are not a whole record
    streams = [tags, sources, times, lengths, payloads, *fields]
    out = bytearray([MODE_PREDICT if predict else MODE_COLUMNS])
    out += varint(count)
    for stream in streams:
        out += varint(len(stream))
    for stream in streams:
        out += stream
    if len(out) > len(chunk):  # a chunk of a few records, as at the end of a segment
        return bytes([MODE_VERBATIM]) + chunk
    return bytes(out)


class _Cursor:
    __slots__ = ("data", "end", "pos")

    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data = data
        self.pos = pos
        self.end = end

    def varint(self) -> int:
        got = read_varint(self.data, self.pos)
        if got is None or got[1] > self.end:
            raise TransformError("a stream ends inside a value")
        self.pos = got[1]
        return got[0]

    def take(self, n: int) -> bytes:
        if self.pos + n > self.end:
            raise TransformError("a stream ends early")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out


def decode(data: bytes) -> bytes:
    """The records ``encode`` was given."""
    if not data:
        raise TransformError("empty")
    mode = data[0]
    if mode == MODE_VERBATIM:
        return data[1:]
    if mode not in (MODE_COLUMNS, MODE_PREDICT):
        raise TransformError(f"unknown transform mode {mode}")
    header = _Cursor(data, 1, len(data))
    count = header.varint()
    sizes = [header.varint() for _ in range(_STREAMS)]
    cursors = []
    pos = header.pos
    for size in sizes:
        cursors.append(_Cursor(data, pos, pos + size))
        pos += size
    if pos != len(data):
        raise TransformError("stream lengths do not add up")
    tags, sources, times, lengths, payloads, *fields = cursors

    def read(i: int, guess: int) -> int:
        return guess + _unzigzag(fields[i].varint())

    last: dict[int, tuple[int, tuple[int, ...]]] = {}
    out = bytearray()
    now = 0
    try:
        for _ in range(count):
            tag = tags.take(1)[0]
            source = sources.varint()
            delta = times.varint()
            now += delta
            if not tag & _CODED:
                payload = payloads.take(lengths.varint())
            elif mode == MODE_PREDICT:
                at, previous = last.get(source, (now, None))
                values = _report(previous, now - at, read)
                last[source] = (now, values)
                payload = _pod_payload(values)
            else:
                payload = _pod_payload(tuple(_unzigzag(f.varint()) for f in fields))
            out += varint(delta) + bytes([tag & ~_CODED]) + varint(source)
            out += varint(len(payload)) + payload
    except TransformError:
        raise
    except (ValueError, IndexError) as exc:
        raise TransformError(f"not a transformed chunk: {exc}") from exc
    return bytes(out)


def sine_table_crc() -> int:
    """The CRC-32 of the sine table, as signed 32-bit little-endian values."""
    return zlib.crc32(b"".join(v.to_bytes(4, "little", signed=True) for v in SINE))
