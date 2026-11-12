from functools import lru_cache

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.http import require_safe

from opencs_web import live
from opencs_web.mapviews import delay_words
from opencs_web.siteassets import ASSETS, asset_bytes, asset_version
from opencs_web.status import Status


@require_safe
async def health(request: HttpRequest) -> HttpResponse:
    """The web process is up. Says nothing about the game server."""
    return HttpResponse("ok\n", content_type="text/plain")


@require_safe
async def home(request: HttpRequest) -> HttpResponse:
    """The status page, rendered with the current status, so it works without JavaScript; the
    script then keeps it live from :func:`status_events`. "Offline" is a page, not an error."""
    status = await live.status_cache().get()
    context = {"status": status, "map_delay": delay_words(settings.OPENCS_MAP_DELAY_PUBLIC)}
    response = render(request, "opencs_web/home.html", context)
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
async def asset(request: HttpRequest, version: str, name: str) -> HttpResponse:
    """A style sheet or script, under the version of the whole set (:func:`asset_version`): a
    browser keeps it for good, and an upgrade changes every address at once, so a page never mixes
    old and new files (``maps.js`` imports ``./maptrack.js`` from the same version). A page from
    before an upgrade asking for the old version gets the current file, not kept."""
    if name not in ASSETS:
        raise Http404
    response = HttpResponse(asset_bytes(name), content_type=ASSETS[name])
    if version == asset_version():
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "no-cache"
    return response
