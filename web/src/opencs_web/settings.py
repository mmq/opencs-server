"""Django settings, built from :mod:`opencs_web.config`."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

from opencs_web import config

try:
    _CONFIG = config.load()
except config.ConfigError as e:
    raise ImproperlyConfigured(str(e)) from e

SECRET_KEY = _CONFIG.secret_key
DEBUG = _CONFIG.debug
ALLOWED_HOSTS = list(_CONFIG.allowed_hosts)

INSTALLED_APPS = ["opencs_web"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
]
ROOT_URLCONF = "opencs_web.urls"
ASGI_APPLICATION = "opencs_web.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _CONFIG.database,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The recording archive (``opencs_web.recordings.archive``): its own SQLite file, not Django's.
OPENCS_RECORDINGS = _CONFIG.recordings
OPENCS_ARCHIVE = _CONFIG.archive
OPENCS_ARCHIVE_MAX_DAYS = _CONFIG.archive_max_days
OPENCS_ARCHIVE_MAX_BYTES = _CONFIG.archive_max_bytes

# The game server's service channel (``opencs_web.channel``) and the live status streams.
OPENCS_SERVICE_SOCKET = _CONFIG.service_socket
OPENCS_MAX_STREAMS = _CONFIG.max_streams
OPENCS_MAX_STREAMS_PER_ADDRESS = _CONFIG.max_streams_per_address

USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"

# Behind Caddy (deploy/Caddyfile), which serves HTTPS, sets X-Forwarded-Proto and passes requests
# to uvicorn on the loopback address; uvicorn trusts the forwarded headers from there alone.
# ``OPENCS_WEB_HTTPS=false`` is for trying the site over plain HTTP on loopback.
HTTPS = _CONFIG.https
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = HTTPS
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # for checks on the loopback address
# A year, for this host only: the operator's other subdomains are not committed to HTTPS, and the
# preload list is a decision for the domain's owner (``check --deploy`` notes both).
SECURE_HSTS_SECONDS = 31_536_000 if HTTPS else 0
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# The pages load their own style sheet and script and nothing else; the script opens the status
# stream. No inline script or style, nothing from elsewhere.
SECURE_CSP = {
    "default-src": [CSP.NONE],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "img-src": [CSP.SELF],
    "base-uri": [CSP.NONE],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
