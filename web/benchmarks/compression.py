"""Compare compressors for the recording archive, per one-minute chunk (the archive's unit).

Run from the repository root, with the benchmark group (zstandard, lz4, brotli)::

    uv run --package opencs-web --group benchmark python web/benchmarks/compression.py \\
        DIR [DIR ...] [--train NAME ...] [--repeat 5] [--memory] [--json results.json]

Every ``*.ocsrec`` under the DIRs is cut into one-minute chunks (``opencs_web.recordings.chunks``).
Dictionaries are trained on the chunks of the ``--train`` segments (by file name; default: the first
segment found) and every candidate is measured on the other chunks, so no candidate sees its test
data in training. For each candidate: compressed size and ratio, compression and decompression
speed (including the record transform, where one is used), the time to seek (decompress one chunk,
undo the transform and decode every message up to the chunk's end, the worst case) and, with
``--memory``, the peak memory of compressing and of decompressing, each in a fresh process.
Every result is checked to decompress to the exact chunk.
"""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import brotli
import lz4.frame
import zstandard

from opencs.messages import DecodeError, decode_client, decode_server
from opencs.recording import Kind
from opencs_web.recordings import transform
from opencs_web.recordings.chunks import Chunk, chunks, read_records

DICT_SIZE = 32 * 1024
SAMPLE_BYTES = 2048
#: xz at preset 6 with a 256 KiB dictionary (a chunk is smaller): the same output, far less memory.
XZ_SMALL = [{"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": 1 << 18}]


@dataclass(frozen=True)
class Candidate:
    name: str
    compress: Callable[[bytes], bytes]
    decompress: Callable[[bytes], bytes]
    #: The record transform applied before compressing: None, "columns" or "predict".
    transform: str | None = None


def _zstd(level: int, dictionary: zstandard.ZstdCompressionDict | None = None) -> Candidate:
    c = zstandard.ZstdCompressor(level=level, dict_data=dictionary, write_content_size=True)
    d = zstandard.ZstdDecompressor(dict_data=dictionary)
    return Candidate(f"zstd-{level}" + ("+dict" if dictionary else ""), c.compress, d.decompress)


def _train(samples: Sequence[bytes], level: int) -> zstandard.ZstdCompressionDict:
    """A dictionary from training chunks, cut into pieces so the trainer has many samples."""
    pieces: list[bytes | bytearray | memoryview] = [
        s[i : i + SAMPLE_BYTES] for s in samples for i in range(0, len(s), SAMPLE_BYTES)
    ]
    return zstandard.train_dictionary(DICT_SIZE, pieces, level=level)


def _plain() -> list[Candidate]:
    return [
        Candidate("gzip-6", lambda d: gzip.compress(d, 6, mtime=0), gzip.decompress),
        Candidate("gzip-9", lambda d: gzip.compress(d, 9, mtime=0), gzip.decompress),
        Candidate("xz-6", lambda d: lzma.compress(d, preset=6), lzma.decompress),
        Candidate("xz-6-256k", lambda d: lzma.compress(d, filters=XZ_SMALL), lzma.decompress),
        Candidate(
            "xz-9e", lambda d: lzma.compress(d, preset=9 | lzma.PRESET_EXTREME), lzma.decompress
        ),
        Candidate("lz4", lz4.frame.compress, lz4.frame.decompress),
        Candidate(
            "lz4-hc",
            lambda d: lz4.frame.compress(d, compression_level=lz4.frame.COMPRESSIONLEVEL_MAX),
            lz4.frame.decompress,
        ),
        Candidate("brotli-5", lambda d: brotli.compress(d, quality=5), brotli.decompress),
        Candidate("brotli-9", lambda d: brotli.compress(d, quality=9), brotli.decompress),
        Candidate("brotli-11", lambda d: brotli.compress(d, quality=11), brotli.decompress),
        *(_zstd(level) for level in (1, 3, 9, 19, 22)),
    ]


def train_dictionaries(train: Sequence[bytes]) -> dict[str, bytes]:
    """zstd dictionaries for levels 3 and 19, on the raw chunks and on the transformed ones."""
    predicted = [transform.encode(c) for c in train]
    out = {}
    for level in (3, 19):
        out[f"raw-{level}"] = _train(train, level).as_bytes()
        out[f"predict-{level}"] = _train(predicted, level).as_bytes()
    return out


def candidates(dictionaries: dict[str, bytes]) -> list[Candidate]:
    def loaded(key: str) -> zstandard.ZstdCompressionDict:
        return zstandard.ZstdCompressionDict(dictionaries[key])

    def transformed(base: Candidate, kind: str) -> Candidate:
        return Candidate(f"{kind}+{base.name}", base.compress, base.decompress, kind)

    plain = _plain()
    out = [*plain, *(_zstd(level, loaded(f"raw-{level}")) for level in (3, 19))]
    out.append(transformed(_zstd(19), "columns"))
    out.append(transformed(next(c for c in plain if c.name == "xz-9e"), "columns"))
    keep = {"gzip-9", "xz-6", "xz-6-256k", "xz-9e", "brotli-11", "lz4-hc"}
    out += [transformed(c, "predict") for c in plain if c.name in keep or c.name.startswith("zstd")]
    out += [transformed(_zstd(level, loaded(f"predict-{level}")), "predict") for level in (3, 19)]
    return out


def _forward(candidate: Candidate, data: bytes) -> bytes:
    if candidate.transform is not None:
        data = transform.encode(data, predict=candidate.transform == "predict")
    return candidate.compress(data)


def _back(candidate: Candidate, data: bytes) -> bytes:
    data = candidate.decompress(data)
    return transform.decode(data) if candidate.transform is not None else data


def _decode_to_end(chunk: Chunk, records: bytes) -> int:
    """What a viewer does after decompressing: every record decoded, up to the chunk's end."""
    count = 0
    for record in read_records(records, chunk.start_ms):
        try:
            if record.kind is Kind.CLIENT:
                decode_client(record.payload)
            elif record.kind in (Kind.SERVER, Kind.SNAPSHOT):
                decode_server(record.payload)
        except DecodeError:
            pass
        count += 1
    return count


@dataclass
class Result:
    name: str
    raw: int
    packed: int
    compress_s: float
    decompress_s: float
    seek_ms_median: float
    seek_ms_max: float
    compress_peak_mb: float | None = None
    decompress_peak_mb: float | None = None

    @property
    def ratio(self) -> float:
        return self.raw / self.packed


def measure(candidate: Candidate, test: Sequence[Chunk], repeat: int) -> tuple[Result, list[bytes]]:
    packed = [_forward(candidate, c.data) for c in test]
    for c, p in zip(test, packed, strict=True):
        if _back(candidate, p) != c.data:
            raise SystemExit(f"{candidate.name}: chunk {c.index} does not come back exactly")
    compress_s = decompress_s = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        for c in test:
            _forward(candidate, c.data)
        compress_s = min(compress_s, time.perf_counter() - t)
        t = time.perf_counter()
        for p in packed:
            _back(candidate, p)
        decompress_s = min(decompress_s, time.perf_counter() - t)
    seeks = []
    for c, p in zip(test, packed, strict=True):
        best = float("inf")
        for _ in range(repeat):
            t = time.perf_counter()
            _decode_to_end(c, _back(candidate, p))
            best = min(best, time.perf_counter() - t)
        seeks.append(best * 1000)
    result = Result(
        candidate.name,
        sum(len(c.data) for c in test),
        sum(len(p) for p in packed),
        compress_s,
        decompress_s,
        statistics.median(seeks),
        max(seeks),
    )
    return result, packed


def _peak_rss() -> int:
    """This process's peak resident size in bytes (ru_maxrss is bytes on macOS, KiB elsewhere)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _memory_child(args: argparse.Namespace) -> None:
    """In a fresh process: the growth of peak memory while one candidate compresses (or
    decompresses) every test chunk. Prints megabytes."""
    _, test = load(args.dirs, args.train)
    dictionaries = {
        k: bytes.fromhex(v) for k, v in json.loads(args.dictionaries.read_text()).items()
    }
    candidate = next(c for c in candidates(dictionaries) if c.name == args.child)
    data = [c.data for c in test]
    if args.child_phase == "decompress":
        data = _unpack(args.packed.read_bytes())  # compressed by the parent: its peak is not here
        step: Callable[[bytes], bytes] = lambda d: _back(candidate, d)  # noqa: E731
    else:
        step = lambda d: _forward(candidate, d)  # noqa: E731
    before = _peak_rss()
    for d in data:
        step(d)
    print((_peak_rss() - before) / 1e6)


def _pack(chunks: Sequence[bytes]) -> bytes:
    return b"".join(len(c).to_bytes(4, "big") + c for c in chunks)


def _unpack(data: bytes) -> list[bytes]:
    out, pos = [], 0
    while pos < len(data):
        n = int.from_bytes(data[pos : pos + 4], "big")
        out.append(data[pos + 4 : pos + 4 + n])
        pos += 4 + n
    return out


def _memory(args: argparse.Namespace, name: str, phase: str, work: Path) -> float:
    argv = [sys.executable, __file__, *map(str, args.dirs), "--child", name]
    argv += ["--child-phase", phase, "--dictionaries", str(work / "dictionaries.json")]
    argv += ["--packed", str(work / "packed.bin")]
    for t in args.train or []:
        argv += ["--train", t]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    return float(out.strip().splitlines()[-1])


def load(
    dirs: Sequence[Path], train_names: Sequence[str] | None
) -> tuple[list[Chunk], list[Chunk]]:
    segments = sorted(p for d in dirs for p in d.rglob("*.ocsrec"))
    if not segments:
        raise SystemExit("no .ocsrec files found")
    names = set(train_names or [segments[0].name])
    train: list[Chunk] = []
    test: list[Chunk] = []
    for path in segments:
        (train if path.name in names else test).extend(chunks(path.read_bytes()))
    if not train or not test:
        raise SystemExit("need both training and test segments")
    return train, test


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("dirs", nargs="+", type=Path, help="directories of .ocsrec segments")
    parser.add_argument("--train", action="append", help="segment file name to train on")
    parser.add_argument("--repeat", type=int, default=5, help="timing runs; the best is kept")
    parser.add_argument("--memory", action="store_true", help="measure peak memory too (slower)")
    parser.add_argument("--only", help="candidates whose name contains this text")
    parser.add_argument("--json", type=Path, help="also write the results here")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--child-phase", help=argparse.SUPPRESS)
    parser.add_argument("--dictionaries", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--packed", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        _memory_child(args)
        return
    train, test = load(args.dirs, args.train)
    raw = sum(len(c.data) for c in test)
    hours = sum(c.last_ms - c.start_ms for c in test) / 3_600_000
    print(
        f"train: {len(train)} chunks, {sum(len(c.data) for c in train)} B; "
        f"test: {len(test)} chunks, {raw} B, {raw / len(test) / 1024:.1f} KiB a chunk, "
        f"{hours * 60:.1f} min, raw {raw / hours / 1e6:.2f} MB/h"
    )
    dictionaries = train_dictionaries([c.data for c in train])
    work = Path(tempfile.mkdtemp())
    (work / "dictionaries.json").write_text(
        json.dumps({k: v.hex() for k, v in dictionaries.items()})
    )
    results = []
    print(
        "| candidate | bytes | MB/h | ratio | compress MB/s | decompress MB/s | ms/chunk c/d "
        "| seek ms med/max | peak MB c/d |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for candidate in candidates(dictionaries):
        if args.only and args.only not in candidate.name:
            continue
        r, packed = measure(candidate, test, args.repeat)
        if args.memory:
            (work / "packed.bin").write_bytes(_pack(packed))
            r.compress_peak_mb = _memory(args, candidate.name, "compress", work)
            r.decompress_peak_mb = _memory(args, candidate.name, "decompress", work)
        results.append(r)
        n = len(test)
        peak = (
            f"{r.compress_peak_mb:.1f} / {r.decompress_peak_mb:.1f}"
            if r.compress_peak_mb is not None and r.decompress_peak_mb is not None
            else "-"
        )
        print(
            f"| {r.name} | {r.packed} | {r.packed / hours / 1e6:.2f} | {r.ratio:.2f} "
            f"| {r.raw / r.compress_s / 1e6:.1f} "
            f"| {r.raw / r.decompress_s / 1e6:.1f} "
            f"| {r.compress_s / n * 1000:.2f} / {r.decompress_s / n * 1000:.2f} "
            f"| {r.seek_ms_median:.1f} / {r.seek_ms_max:.1f} | {peak} |",
            flush=True,
        )
    shutil.rmtree(work)
    if args.json:
        rows = [{**vars(r), "ratio": r.ratio} for r in results]
        args.json.write_text(json.dumps({"test_chunks": len(test), "results": rows}, indent=1))


if __name__ == "__main__":
    main()
