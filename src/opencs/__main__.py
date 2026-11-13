"""``opencs [--config PATH] [--port N] [--host H] [--web]``, or ``python -m opencs``."""

import sys

from opencs.app import main


def run() -> None:
    try:
        code = main()
    except KeyboardInterrupt:
        # Ctrl-C before the server takes SIGINT over (or after it let go): nothing is open to save.
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    run()
