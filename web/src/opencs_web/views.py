from functools import lru_cache
from importlib.resources import files

from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.http import require_safe

from opencs_web import live
from opencs_web.status import Status

#: The page's own style sheet and script, served from the package: no external assets.
ASSETS = {
    "site.css": "text/css; charset=utf-8",
    "live.js": "text/javascript; charset=utf-8",
}


@require_safe
async def health(request: HttpRequest) -> HttpResponse:
    """The web process is up. Says nothing about the game server."""
    return HttpResponse("ok\n", content_type="text/plain")


@require_safe
async def home(request: HttpRequest) -> HttpResponse:
    """The status page, rendered with the current status, so it works without JavaScript; the
    script then keeps it live from :func:`status_events`. "Offline" is a page, not an error."""
    status = await live.status_cache().get()
    response = render(request, "opencs_web/home.html", {"status": status})
    response["Cache-Control"] = "no-cache"
    return response


@require_safe
async def status_events(request: HttpRequest) -> StreamingHttpResponse:
    """The live status: server-sent events, each the status block as HTML."""
    stream = live.status_events(
        live.status_cache(),
        live.streams(),
        request.META.get("REMOTE_ADDR") or "?",
        render_status,
    )
    response = StreamingHttpResponse(stream, content_type="text/event-stream")
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    return response


@lru_cache(maxsize=8)
def render_status(status: Status) -> str:
    """The status block, rendered once per status however many streams send it. Every callsign
    and name goes through the template's autoescaping."""
    return render_to_string("opencs_web/status.html", {"status": status})


@require_safe
async def asset(request: HttpRequest, name: str) -> HttpResponse:
    if name not in ASSETS:
        raise Http404
    response = HttpResponse(_asset(name), content_type=ASSETS[name])
    response["Cache-Control"] = "public, max-age=3600"
    return response


@lru_cache
def _asset(name: str) -> bytes:
    return files("opencs_web").joinpath("assets", name).read_bytes()
