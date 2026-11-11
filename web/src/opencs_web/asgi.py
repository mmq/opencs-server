"""The ASGI application, served by one uvicorn worker:
``uvicorn opencs_web.asgi:application``."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "opencs_web.settings")

application = get_asgi_application()
