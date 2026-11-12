# Contributing to OpenCS

Thanks for helping. Bug reports, fixes and documentation corrections are all welcome. For anything larger
than a fix, open an issue first so the approach can be agreed before you write it.

## Setup

Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/mmq/opencs-server.git
cd opencs-server
uv sync                      # create .venv with the server and the dev tools
```

## The checks

Every commit passes all of these (CI runs them on Python 3.12 and 3.15 on Linux, and 3.15 on macOS):

```bash
uv run pytest                              # the tests, under a minute
uv run ruff check . && uv run ruff format --check .
uv run mypy                                # strict, on src/
```

`tools/smoke.sh` starts a throwaway server and runs the 19 judge checks (about a minute). Run it after a
change to the network, session or world code.

## A fix comes with a test

A bug fix comes with a test that fails without the fix, and a new rule comes with its test, in the same
commit. Keep one change to a commit, with a message that says what a player or operator sees. If a change
alters a limit or a claim, update the docs that state it (`docs/robustness.md`, `docs/OPERATING.md`,
`SECURITY.md`, `CHANGELOG.md`).

Values the server chooses go in `src/opencs/policy.py`, each with a comment giving the reason. The
conventions are in [`docs/DESIGN.md`](docs/DESIGN.md) section 8, and the layers a module may import from
are in section 3.

## Citing the specification

OpenCS implements the [CyberStrike Server Specification](https://github.com/mmq/cyberstrike-spec/tree/v1.0),
version 1.0. Cite it in docstrings, tests and commit messages as `I §n`, `II §n`, `III §n` or `IV §n`
(the part, then the section number), and say "spec v1.0" when a message names the specification. A behavior
the specification does not settle is a choice: put it in `policy.py` and say so.

## Game files

The game's own files (its program, maps and manual) never go into the repository, nor does anything copied
from them. `.gitignore` keeps the usual ones out, and a test fails if one is tracked.

## Security reports

Do not open a public issue for a vulnerability. Report it privately as [`SECURITY.md`](SECURITY.md)
describes.
