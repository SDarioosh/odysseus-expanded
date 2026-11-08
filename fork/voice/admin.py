"""Fork: "a signed-in admin only", the one check of the voice pages' admin routes.

:func:`signed_in_admin` returns the owner of a browser session of an Odysseus admin. An
API token or the tool loopback is refused (``refused``), then ``require_admin`` runs (its
own 403, or ``not_admin`` when given; skipped with ``admin_checked``, for a route that
has it as a dependency), then a request with no user is refused (``not_signed_in``).
``error`` makes each refusal, ``HTTPException`` unless the caller has its own
(``fork/voice/echo_link.py``'s ``LinkError``).
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException, Request


def signed_in_admin(request: Request, refused: str, *, not_admin: str | None = None,
                    not_signed_in: str = "Not signed in", admin_checked: bool = False,
                    error: Callable[[int, str], Exception] = HTTPException) -> str:
    from core.middleware import require_admin
    from src.agent_runtime.authority import is_internal_tool_request
    from src.auth_helpers import get_current_user

    if getattr(request.state, "api_token", False) or is_internal_tool_request(request):
        raise error(403, refused)
    if not admin_checked and not_admin is None:
        require_admin(request)
    elif not admin_checked:
        try:
            require_admin(request)
        except HTTPException:
            raise error(403, not_admin) from None
    owner = get_current_user(request)
    if not owner:
        raise error(403, not_signed_in)
    return str(owner)
