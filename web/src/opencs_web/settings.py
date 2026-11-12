"""Django settings, built from :mod:`opencs_web.config`."""

from __future__ import annotations

from typing import Any

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

# Django's users and django-otp are for the admin pages only (``staff``): an admin is a user with
# an ``AdminAccount``, a password and a TOTP device. Django's own admin site is not installed.
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "opencs_web",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
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
        "OPTIONS": {"context_processors": ["opencs_web.accountviews.site"]},
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

# Registration and web accounts (``opencs_web.accounts``).
OPENCS_SITE_URL = _CONFIG.site_url
OPENCS_REGISTRATION = _CONFIG.registration
OPENCS_PASSWORD_DELIVERY = _CONFIG.password_delivery
OPENCS_CHOSEN_PASSWORDS = _CONFIG.chosen_passwords
OPENCS_CONTACT = _CONFIG.contact
OPENCS_PRIVACY_FILE = _CONFIG.privacy_file
OPENCS_EMAIL_DAILY_CAP = _CONFIG.email_daily_cap
#: A form sent back sooner than this after it was shown is taken for a script's (the challenge).
OPENCS_FORM_MIN_SECONDS = 3.0

# Email: through an SMTP server (Resend's, or any), or printed to the log when none is set, so
# development needs no provider. The tests use Django's in-memory backend.
DEFAULT_FROM_EMAIL = _CONFIG.email_from
MAILERS: dict[str, dict[str, Any]]
if _CONFIG.email is None:
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
else:
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": _CONFIG.email.host,
                "port": _CONFIG.email.port,
                "username": _CONFIG.email.user,
                "password": _CONFIG.email.password,
                "use_tls": _CONFIG.email.security == "starttls",
                "use_ssl": _CONFIG.email.security == "ssl",
                "timeout": 10,
            },
        }
    }

# The admin pages (``staff``): off unless turned on, under their own path. An admin's log-in lasts
# at most ``OPENCS_ADMIN_SESSION_MINUTES``, and less if idle (``staff.IDLE_SECONDS``).
OPENCS_ADMIN = _CONFIG.admin
OPENCS_ADMIN_PATH = _CONFIG.admin_path
OPENCS_ADMIN_SESSION_MINUTES = _CONFIG.admin_session_minutes
# Admin passwords: Argon2id, Django's own hasher (argon2-cffi is the server's dependency already).
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
OTP_TOTP_ISSUER = "OpenCS"

# A web account's session: in the web's database, so logging out ends it. Two weeks.
SESSION_COOKIE_AGE = 14 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

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
