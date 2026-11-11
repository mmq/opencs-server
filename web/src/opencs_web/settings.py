"""Django settings, built from :mod:`opencs_web.config`."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

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
]
ROOT_URLCONF = "opencs_web.urls"
ASGI_APPLICATION = "opencs_web.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _CONFIG.database,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
