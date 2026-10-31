"""Fork: keep the Qwen thinking switch working on private endpoints.

``_apply_local_qwen_thinking_mode`` only sends ``enable_thinking`` to endpoints
that ``is_local_endpoint`` calls local. That check returns False for an endpoint
configured (or guessed) as ``api`` or ``proxy``, and an endpoint that has an
API key and a ``/v1`` URL on any port but 11434 is guessed to be a proxy. A
local llama-server run with a key was therefore sent no ``enable_thinking``, and
"Thinking: Off" had no effect: the model spent its reply on thinking and
returned no answer.

A private host (loopback, LAN, Tailscale) is the owner's own machine whatever
its kind, so the switch is sent there too. Hosted providers still do not
receive the field.
Hook: ``src/llm_core.py`` (``_apply_local_qwen_thinking_mode``).
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_private_host(url: str) -> bool:
    from fork.upstream import is_local_or_private_host
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and is_local_or_private_host(host)
