"""The settings the web tests (and mypy's Django plugin) use: the production settings with a
throwaway secret key and an in-memory database."""

import os

os.environ.setdefault("OPENCS_WEB_SECRET_KEY", "test-only-not-a-secret")
os.environ.setdefault("OPENCS_WEB_DATABASE", ":memory:")
os.environ.setdefault("OPENCS_WEB_HTTPS", "false")  # the test client speaks plain HTTP

from opencs_web.settings import *  # noqa: F403
