# 15. Archived recordings are one-minute chunks, transformed, then compressed with xz

## Context

The archiver in `web/` turns the recorder's segments (ADR 14) into an archive: a SQLite file of
compressed one-minute chunks with an index, from which the web process decodes a time window. ADR 14 left
the compression method to be chosen by measuring real recordings.

The pod state reports (`'b'`) are 96.5% of a busy city's bytes, and a general-purpose compressor does
poorly on them: the numbers that change (position, heading, speed, turn rate) are mixed with ones that
hardly change, inside short records. `web/benchmarks/compression.py` compares gzip, xz, zstd (levels 1 to
22, with and without a trained dictionary), lz4 and brotli per one-minute chunk, on the raw records and on
two lossless transforms of them: the records split into a stream per field (`columns`), and the same with
each pod report stored as its difference from what the pilot's previous report predicts (`predict`).

Measured on two 16-bot games in city B (bots report 2 times a second in one and once a second in the
other, as the real client does; dictionaries trained on each game's first segment and tested on the rest;
Apple M3 Pro, Python 3.13):

| method | 2/s: ratio | MB an hour | 1/s: ratio | MB an hour | 2/s: ms a chunk, in / out | 2/s: peak MB, in / out |
|---|---|---|---|---|---|---|
| none | 1 | 2.87 | 1 | 1.44 | | |
| gzip-6 | 2.52 | 1.14 | 2.41 | 0.60 | 0.8 / 0.05 | 0.1 / 0.1 |
| zstd-19 | 2.82 | 1.02 | 2.62 | 0.55 | 5.8 / 0.04 | 1.8 / 0.1 |
| zstd-19, dictionary | 2.86 | 1.00 | 2.68 | 0.54 | 4.0 / 0.05 | 3.0 / 0.1 |
| xz-6 (256 KiB) | 3.13 | 0.92 | 2.77 | 0.52 | 4.4 / 0.5 | 1.6 / 0.2 |
| predict, zstd-19 | 4.97 | 0.58 | 3.87 | 0.37 | 32 / 20 | 2.0 / 0.2 |
| predict, brotli-11 | 5.73 | 0.50 | 4.37 | 0.33 | 55 / 20 | 3.4 / 0.2 |
| **predict, xz-6 (256 KiB)** | **5.48** | **0.52** | **4.17** | **0.35** | **31 / 20** | **1.7 / 0.3** |

The transform is what matters: it nearly halves the archive whatever compresses it after. Its pure-Python
cost (about 30 ms to code a minute of a busy city, 20 ms to decode it) is far larger than any of these
compressors', so their speed differences no longer show. A trained dictionary gains at most 2.5% on the
raw records and under 1% on the transformed ones: a chunk of 20 to 40 KiB is long enough for the
compressor to learn its own context.

## Decision

A chunk is the records of one minute of a segment, byte for byte (`opencs_web.recordings.chunks`).
It is stored as `xz(predict(chunk))`:

- `predict` is `opencs_web.recordings.transform.encode` (mode 2): streams per field; a pod report's
  position predicted by dead reckoning (the mean of the two reports' speed along the mean of their
  headings), its heading by the mean turn rate, `v2` by `v1`, every other field by the previous report.
  Integer arithmetic only, with a sine table pinned by a CRC. A report is coded only if it encodes back to
  its exact bytes; anything else is kept as it is, so `decode(encode(c)) == c` for any bytes.
- `xz` is the standard library's `lzma`: the `.xz` format with LZMA2 at preset 6 and a 256 KiB
  dictionary (a chunk is smaller, so the output is the same as with preset 6's 8 MiB, in a tenth of the
  memory), and its integrity check.
- No trained dictionary. Each chunk's row names its transform mode (the first byte of the decompressed
  data) and its compressor, so a later choice can sit beside this one in the same archive.

brotli-11 is 4 to 5% smaller, and zstd faster, but neither difference is worth a third-party package
while the transform dominates the time; xz needs none.

## Consequences

- The archiver needs nothing outside the standard library and `opencs`. `zstandard`, `lz4` and `brotli`
  are in `web/`'s `benchmark` dependency group only, for rerunning the benchmark.
- A busy city's archive grows by about 0.35 MB an hour at the real client's report rate, 0.5 MB at twice
  that (16 bots). Bots move more regularly than people, so people's reports will be predicted less well;
  without the transform's gain, xz alone gives 0.5 to 0.9 MB an hour, which bounds it.
- The transform is part of the archive format. Mode 2 must decode the same forever: a change to the
  prediction is a new mode, and `decode` keeps every old one. The sine table's CRC is tested, so a
  platform that builds it differently fails the tests instead of decoding wrongly.
- Seeking to a point costs one chunk: about 20 ms to decompress and undo the transform for a busy minute,
  plus decoding the messages up to the point (about 12 ms for a whole minute).
- Segments run a little over five minutes, so the last few records of each would make a tiny sixth
  chunk; the archiver folds a last chunk that lies within the first 10 seconds of its minute into the
  chunk before it.
- In the archive (`opencs_web.recordings.archive`) this choice is codec 1 in each chunk's row, beside
  the transform's mode.
