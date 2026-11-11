from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_safe


@require_safe
async def health(request: HttpRequest) -> HttpResponse:
    """The web process is up. Says nothing about the game server."""
    return HttpResponse("ok\n", content_type="text/plain")
