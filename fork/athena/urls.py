"""Fork: Athena's URLs and token scopes in the hub, the Athena names and the old ones.

The Athena names came later (they were ``sound``). The old ones keep working for good:
installed phone relays call ``/api/codex/sound/*`` with ``sound:*`` tokens, and tabs and
bookmarks hold ``/modules/sound…`` and ``/api/modules/sound/*``.

* Pages: ``/modules/athena``, ``/modules/athena/record`` and ``/modules/athena/pair`` serve
  the pages; ``/modules/sound…`` (and the older ``/barvis/sound``) redirect there, with the
  query string. A ``#c=<id>`` deep link stays in the browser across the redirect.
* API (``/api/modules/…``) and phone relay (``/api/codex/…``): each route is served at the
  Athena path and the old one by the same handler (:func:`add_aliases`), never through a
  redirect, so POST bodies and phone clients see no difference.
* Scopes: each relay check accepts the Athena scope or the old one (:data:`INGEST` …), and
  a new pairing gets both (``phone_pairing.RELAY_SCOPES``). Existing tokens are never
  rewritten.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute

PAGE = "/modules/athena"
OLD_PAGE = "/modules/sound"
FORMER_PAGE = "/barvis/sound"  # before /modules/sound
API = "/api/modules/athena"
OLD_API = "/api/modules/sound"
RELAY = "/api/codex/athena"
OLD_RELAY = "/api/codex/sound"

# the relay's scopes: Athena name, old name
INGEST = {"athena:ingest", "sound:ingest"}
READ = {"athena:read", "sound:read"}
WRITE = {"athena:write", "sound:write"}
SCOPES = ["athena:ingest", "athena:read", "athena:write"]
OLD_SCOPES = ["sound:ingest", "sound:read", "sound:write"]

# Not 301: a browser keeps a permanent redirect, and after a rollback to a hub without
# /modules/athena the old address would then lead nowhere until its cache is cleared.
REDIRECT_STATUS = 302


def redirect(request: Request, path: str) -> RedirectResponse:
    """A redirect to ``path`` on this hub, keeping the query string."""
    query = request.url.query
    return RedirectResponse(f"{path}?{query}" if query else path,
                            status_code=REDIRECT_STATUS)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def add_aliases(router: APIRouter, old: str, new: str) -> None:
    """Serve every route of ``router`` under ``old`` at ``new`` too: the same endpoint,
    dependencies, methods and options, added in the routers' order (so path matching
    picks the same handler under both prefixes). Call it once the routes are defined."""
    for route in list(router.routes):
        if not isinstance(route, APIRoute) or not _under(route.path, old):
            continue
        extra: dict[str, Any] = {}
        if hasattr(route, "strict_content_type"):
            extra["strict_content_type"] = route.strict_content_type
        router.add_api_route(
            new + route.path[len(old):],
            route.endpoint,
            response_model=route.response_model,
            status_code=route.status_code,
            # add_api_route adds the router's own tags and dependencies again
            tags=[t for t in route.tags if t not in router.tags],
            dependencies=list(route.dependencies)[len(router.dependencies):],
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            deprecated=route.deprecated,
            methods=route.methods,
            operation_id=route.operation_id,
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            include_in_schema=route.include_in_schema,
            response_class=route.response_class,
            name=route.name,
            callbacks=route.callbacks,
            openapi_extra=route.openapi_extra,
            generate_unique_id_function=route.generate_unique_id_function,
            **extra,
        )
