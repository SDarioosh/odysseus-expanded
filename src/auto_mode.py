"""Fork: auto mode, safe tools without approval after web content.

Upstream arms an approval gate once outside content (a search result, a fetched
page, an email) enters a run: from then on every tool that could change things
or send data out waits for the owner (``ToolRunSecurityContext.decision_for``
in ``src/tool_capabilities.py``). ``web_fetch`` counts as sending data out,
because the model picks the URL and a URL can carry data. So "look up this
site" asked for approval on the second step, and so did reading the owner's
own notes after a search.

Each chat has an Auto/Ask button in the composer (``static/js/forkAutoMode.js``);
a chat that never chose follows the default in Settings → Tools (Auto unless the
admin picked Ask). In Auto the gate lets these through without asking:

* tools whose only effects are reads (web reads, workspace reads, the owner's
  own stores, asking the user), as classified in ``src/tool_capabilities.py``;
* ``web_fetch`` of a URL that can't carry data the model picked, or only a short
  path to a site the search chose:
  - a URL in the owner's own message (or a page on the same site, with no
    query string; on GitHub only inside the same owner/repo), or
  - a URL that came back in a tool result (a search hit, a link on a fetched
    page), on a public host name (no IP address, no local name). ``www.`` and a
    trailing ``/`` don't count as a difference, or
  - a page with no query string on a site a web search returned in this run
    (on GitHub only inside the same owner/repo). Search hits often carry
    ``?variant=`` or a ``/collections/`` path, and the model asks for the clean
    product page; that page was asked for approval although its site was a hit.
    Only the ranked hits count, not links inside pages, so a page can't add a
    site (such as a request catcher) that any path on it would then reach.
* ``calculate``, the calculator below: arithmetic only, no code, no network.

Everything else keeps asking: email, notes/tasks/calendar writes, deletes,
terminal and code, file writes, admin changes, unknown tools, and a fetch of a
URL the model made up on a site no search returned. Token-driven (delegated) runs
never get auto mode.

Upstream lets the browser, ``pdf_extract`` and ``youtube_tool`` open any address
after outside content, because the run can't load new private data at that
point. Auto mode can (it lets the owner's own stores be read), so in auto mode
those tools follow the same known-URL rule as ``web_fetch``, and after a private
read the browser's interactive actions (click, fill, script ...) ask.
Policy, per-tool disables and request authority still apply before the gate.

Hooks: ``src/tool_capabilities.py`` (gate, URL tracking, calculator
capability), ``src/tool_types.py`` and ``src/agent_tools/__init__.py``
(calculator tool), ``routes/chat_routes.py`` (the chat's choice, calculator
grant and offer), ``src/agent_loop.py`` (binds the choice to the run),
``app.py`` (default route), ``static/index.html`` (button and settings card).
"""

from __future__ import annotations

import ast
import ipaddress
import json
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Default for chats that don't say (new chats, API callers). The first version
# stored a global on/off as ``fork_auto_mode`` (default off); that key is ignored.
SETTING_KEY = "fork_auto_mode_default"
CALCULATOR_TOOL = "calculate"
CHAT_MODE_FIELD = "fork_tool_mode"  # "auto" or "ask", sent by the composer button

# ── Setting ─────────────────────────────────────────────────────────────────


def enabled() -> bool:
    """The default for chats: auto unless the admin chose "ask"."""
    try:
        from src.settings import get_setting
        return get_setting(SETTING_KEY, True) is not False
    except Exception:
        return True


def set_enabled(value: bool) -> bool:
    from src.settings import load_settings, save_settings
    settings = dict(load_settings())
    settings[SETTING_KEY] = bool(value)
    save_settings(settings)
    return bool(value)


# ── Per-chat choice ─────────────────────────────────────────────────────────
# The composer's Auto/Ask button sends ``fork_tool_mode`` with every message of
# a chat (approvals included), so the latest choice is kept in memory per
# (user, chat); after a restart the next message brings it back.

_CHAT_MODES: dict[tuple[str, str], bool] = {}
_MAX_CHATS = 5000


def remember_chat_mode(owner: Any, session_id: Any, value: Any) -> None:
    mode = str(value or "").strip().lower()
    if not session_id or mode not in {"auto", "ask"}:
        return
    key = (str(owner or ""), str(session_id))
    _CHAT_MODES.pop(key, None)
    _CHAT_MODES[key] = mode == "auto"
    while len(_CHAT_MODES) > _MAX_CHATS:
        _CHAT_MODES.pop(next(iter(_CHAT_MODES)))


def chat_enabled(owner: Any = None, session_id: Any = None) -> bool:
    """Auto mode for this chat: its own choice, else the default."""
    if session_id:
        choice = _CHAT_MODES.get((str(owner or ""), str(session_id)))
        if choice is not None:
            return choice
    return enabled()


def bind_run(ctx, *, owner: Any = None, session_id: Any = None) -> None:
    """Fix the run's mode when the agent loop builds its security context."""
    try:
        object.__setattr__(ctx, "_fork_auto_mode", chat_enabled(owner, session_id))
    except (AttributeError, TypeError):
        pass


def run_enabled(ctx) -> bool:
    """Only runs the agent loop bound to a chat get Auto; any other use of the
    gate (previews, direct callers, upstream's own tests) keeps upstream's rules."""
    return getattr(ctx, "_fork_auto_mode", None) is True


# ── URLs seen in the run ────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s<>\"'`\\\]\[}{|^]+", re.I)
# A bare "github.com/owner/repo" or "example.com" the owner typed.
_BARE_RE = re.compile(
    r"(?<![\w@./:-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
    r"(?:/[^\s<>\"'`\\\]\[}{|^]*)?)",
    re.I,
)
_TRAILING = ".,;:!?*_~"
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home", ".arpa", ".ts.net",
                   ".localhost", ".localdomain", ".intranet", ".corp")
# Hosts that serve everyone's content: a site match must stay in one owner/repo.
_GITHUB_HOSTS = frozenset({"github.com", "raw.githubusercontent.com"})
_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be", "youtube-nocookie.com"})
_MAX_URLS = 2000


def _state(ctx) -> dict:
    state = getattr(ctx, "_fork_auto_urls", None)
    if state is None:
        state = {"user": set(), "result": set(), "search": set()}
        try:
            object.__setattr__(ctx, "_fork_auto_urls", state)
        except (AttributeError, TypeError):
            pass
    return state


def normalize_url(raw: Any) -> str | None:
    """``https://host/path?query`` (lower-case host, no fragment), or None."""
    text = str(raw or "").strip().strip("<>")
    while text and (text[-1] in _TRAILING
                    or (text[-1] == ")" and text.count(")") > text.count("("))):
        text = text[:-1]
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower().rstrip(".")
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{netloc}{path}{query}"


def _urls_in(text: str, *, bare: bool) -> set[str]:
    found = {normalize_url(m.group(0)) for m in _URL_RE.finditer(text)}
    if bare:
        scrubbed = _URL_RE.sub(" ", text)
        found |= {normalize_url(m.group(1)) for m in _BARE_RE.finditer(scrubbed)}
    return {u for u in found if u}


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # content parts
        return "\n".join(_text_of(part.get("text") if isinstance(part, dict) else part)
                         for part in value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _add(bucket: set[str], urls: Iterable[str]) -> None:
    for url in urls:
        if len(bucket) >= _MAX_URLS:
            return
        bucket.add(url)


def observe_messages(ctx, messages: Iterable[dict]) -> None:
    """Note URLs the owner wrote and URLs that came back in earlier tool results."""
    state = _state(ctx)
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        role = message.get("role")
        text = _text_of(message.get("content"))
        if not text:
            continue
        if metadata.get("trusted") is False or role == "tool":
            _add(state["result"], _urls_in(text, bare=False))
        elif role == "user":
            _add(state["user"], _urls_in(text, bare=True))


# web_search appends its ranked hits as ``<!-- SOURCES:[{"url": ...}] -->`` at the
# very end. Page text quoted earlier in the output could imitate the marker, so
# only the final block counts.
_SOURCES_RE = re.compile(r"<!-- SOURCES:(\[[^\n]*\]) -->\s*\Z")
# Longer paths on a search site still ask: a long path can carry data.
_MAX_SEARCH_SITE_PATH = 200


def _search_hits(result: dict) -> set[str]:
    hits: set[str] = set()
    match = _SOURCES_RE.search(str(result.get("output") or ""))
    if match is None:
        return hits
    try:
        items = json.loads(match.group(1))
    except (TypeError, ValueError):
        return hits
    for item in items if isinstance(items, list) else ():
        url = normalize_url(item.get("url") if isinstance(item, dict) else None)
        if url and public_host(_host(url)):
            hits.add(url)
    return hits


def observe_tool_result(ctx, result: Any, tool_name: Any = None) -> None:
    if isinstance(result, dict):
        state = _state(ctx)
        _add(state["result"], _urls_in(_text_of(result), bare=False))
        if tool_name == "web_search":
            _add(state.setdefault("search", set()), _search_hits(result))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def public_host(host: str) -> bool:
    """A real public name: has a dot, isn't an IP address or a local name."""
    host = (host or "").lower().rstrip(".")
    if not host or "." not in host or host == "localhost":
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    return not host.endswith(_LOCAL_SUFFIXES)


def _site(url: str) -> tuple[str, tuple[str, ...]]:
    """(site, scope): the host, plus owner/repo on GitHub hosts."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _GITHUB_HOSTS:
        segs = tuple(s.casefold() for s in parts.path.split("/") if s)[:2]
        return "github", segs
    return host, ()


def same_site(url: str, owner_url: str) -> bool:
    site, scope = _site(url)
    owner_site, owner_scope = _site(owner_url)
    if site != owner_site:
        return False
    return scope[:len(owner_scope)] == owner_scope


def _loose(url: str) -> str:
    """``url`` without ``www.`` and a trailing ``/``: the same page either way."""
    parts = urlsplit(url)
    netloc = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{netloc}{path}{query}"


def fetch_url_allowed(ctx, url: str) -> bool:
    normalized = normalize_url(url)
    if normalized is None:
        return False
    state = _state(ctx)
    loose = _loose(normalized)
    if normalized in state["user"] or loose in {_loose(u) for u in state["user"]}:
        return True
    parts = urlsplit(normalized)
    if not parts.query and any(same_site(normalized, u) for u in state["user"]):
        return True
    if not public_host(_host(normalized)):
        return False
    if normalized in state["result"] or loose in {_loose(u) for u in state["result"]}:
        return True
    return (not parts.query and len(parts.path) <= _MAX_SEARCH_SITE_PATH
            and any(same_site(normalized, u) for u in state.get("search", ())))


def fetch_urls(content: Any) -> list[str] | None:
    """The URLs a ``web_fetch`` call would read, or None when unclear."""
    payload: Any = content
    if isinstance(content, str):
        raw = content.strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                return None
        else:
            first = raw.split("\n", 1)[0].strip()
            return [first] if first else None
    if not isinstance(payload, dict):
        return None
    if "urls" in payload:
        items = payload.get("urls")
        if not isinstance(items, list) or not items:
            return None
        urls = [item.get("url") if isinstance(item, dict) else item for item in items]
    else:
        urls = [payload.get("url")]
    urls = [str(u).strip() for u in urls if isinstance(u, str) and u.strip()]
    return urls or None


# ── The gate ────────────────────────────────────────────────────────────────


# Tools that open a web address the model passes. Upstream lets the last three run after
# outside content because the run can't load new private data then; auto mode can, so
# in auto mode all four open only known addresses without asking (web_fetch's rule).
URL_TOOLS = frozenset({"web_fetch", "pdf_extract", "youtube_tool", "private_browser"})
# Browser actions that don't open an address the model typed. Interactive ones (click,
# fill, press, evaluate ...) stay upstream's call until auto mode let a private read
# through in this run; then they ask.
_BROWSER_QUIET_ACTIONS = frozenset({"snapshot", "read", "text", "find", "screenshot", "scroll",
                                    "wait", "status", "tabs", "session_info", "close",
                                    "close_page", "back", "forward", "reload"})
_BROWSER_OPEN_ACTIONS = frozenset({"open", "navigate", "new_page", "goto"})
STILL_ASKS = ("Auto mode: only web addresses from your message or from earlier results "
              "open without asking.")


def _payload(content: Any) -> dict:
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def url_tool_allowed(ctx, tool_name: str, content: Any) -> bool:
    """A URL-taking call that can't carry data the model picked."""
    if tool_name == "web_fetch":
        urls = fetch_urls(content)
        return bool(urls) and all(fetch_url_allowed(ctx, u) for u in urls)
    payload = _payload(content)
    if tool_name == "pdf_extract":
        url = str(payload.get("url") or "").strip()
        if not url:
            return bool(payload.get("path"))  # a task-local file, no network
        if url.startswith("/workspace/") or url.lower().startswith("file://"):
            return True
        return fetch_url_allowed(ctx, url)
    if tool_name == "youtube_tool":
        # Ids and handles only reach YouTube; an address elsewhere follows the rule.
        urls = ([payload.get(k) for k in ("url", "video_url", "channel_url")] if payload
                else [str(content or "").strip().split("\n", 1)[0]])
        for url in (str(u).strip() for u in urls if u):
            host = _host(normalize_url(url) or "")
            if not (host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")) \
                    and not fetch_url_allowed(ctx, url):
                return False
        return True
    if tool_name == "private_browser":
        action = str(payload.get("action") or "").strip().lower()
        if action in _BROWSER_QUIET_ACTIONS:
            return True
        url = str(payload.get("url") or "").strip()
        return action in _BROWSER_OPEN_ACTIONS and bool(url) and fetch_url_allowed(ctx, url)
    return False


def gate_decision(ctx, tool_name: Any, content: Any = None) -> tuple[bool, str | None] | None:
    """Auto mode's answer for a call after outside content, or None for upstream's.

    ``(True, None)`` runs it without asking, ``(False, reason)`` asks, ``None`` leaves
    the call to upstream's gate (which asks for anything that writes or sends).
    """
    if not isinstance(tool_name, str) or getattr(ctx, "delegated_credential", False):
        return None
    if not run_enabled(ctx):
        return None
    from src.tool_capabilities import ToolEffect, capabilities_for_action

    state = _state(ctx)
    if tool_name in URL_TOOLS:
        action = str(_payload(content).get("action") or "").strip().lower()
        if tool_name == "private_browser" and action not in _BROWSER_OPEN_ACTIONS:
            if action in _BROWSER_QUIET_ACTIONS or not state.get("private_read"):
                return None
            return False, STILL_ASKS
        if url_tool_allowed(ctx, tool_name, content):
            logger.info("[auto-mode] allowed %s without approval", tool_name)
            return True, None
        return False, STILL_ASKS

    caps = capabilities_for_action(tool_name, content)
    if not caps.known:
        return None
    reads = {ToolEffect.READ_PUBLIC, ToolEffect.READ_WORKSPACE, ToolEffect.READ_PRIVATE,
             ToolEffect.BROKERED_NETWORK_READ, ToolEffect.USER_INTERACTION}
    effects = set(caps.effects)
    if effects <= reads:
        if ToolEffect.READ_PRIVATE in effects:
            state["private_read"] = True
        logger.info("[auto-mode] allowed %s without approval", tool_name)
        return True, None
    return None


def allows(ctx, tool_name: Any, content: Any = None) -> bool:
    verdict = gate_decision(ctx, tool_name, content)
    return bool(verdict and verdict[0])


# ── Calculator ──────────────────────────────────────────────────────────────

CALCULATOR_SCHEMA = {
    "type": "function",
    "function": {
        "name": CALCULATOR_TOOL,
        "description": (
            "Exact arithmetic. Use it for every number you compute (sums, rates, unit "
            "conversions, airtime, percentages) instead of working it out in your head. "
            "Python syntax for numbers only: + - * / // % **, parentheses, and math "
            "functions sqrt, log, log2, log10, exp, sin, cos, tan, asin, acos, atan, "
            "atan2, hypot, ceil, floor, abs, round, min, max, factorial, radians, "
            "degrees; constants pi, e, tau. No variables or code. Pass several "
            "expressions at once in expressions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "One expression, e.g. \"8*39 - 4*7 + 28 + 16\""},
                "expressions": {"type": "array", "maxItems": 20, "items": {"type": "string"},
                                "description": "Several expressions, evaluated in order"},
            },
            "required": [],
        },
    },
}

_MAX_EXPR_CHARS = 500
_MAX_INT_BITS = 4096
_MAX_FACTORIAL = 1000

_FUNCS = {
    name: getattr(math, name)
    for name in ("sqrt", "log", "log2", "log10", "exp", "sin", "cos", "tan", "asin", "acos",
                 "atan", "atan2", "hypot", "ceil", "floor", "radians", "degrees")
}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max})
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}


class CalcError(ValueError):
    pass


def _check(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalcError("result is not a number")
    if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
        raise CalcError("number too large")
    return value


def _power(base, exp):
    if isinstance(base, int) and isinstance(exp, int) and exp > 0:
        if abs(base) > 1 and exp * max(abs(base).bit_length(), 1) > _MAX_INT_BITS:
            raise CalcError("number too large")
    try:
        return base ** exp
    except OverflowError as exc:
        raise CalcError("number too large") from exc


_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: _power,
}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        return _check(node.value)
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise CalcError(f"unknown name {node.id!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int):
            if left.bit_length() + right.bit_length() > _MAX_INT_BITS:
                raise CalcError("number too large")
        try:
            return _check(_BINOPS[type(node.op)](left, right))
        except ZeroDivisionError as exc:
            raise CalcError("division by zero") from exc
        except OverflowError as exc:
            raise CalcError("number too large") from exc
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        name = node.func.id
        args = [_eval(arg) for arg in node.args]
        if name == "factorial":
            if len(args) != 1 or not isinstance(args[0], int) or not 0 <= args[0] <= _MAX_FACTORIAL:
                raise CalcError(f"factorial takes one whole number 0..{_MAX_FACTORIAL}")
            return math.factorial(args[0])
        if name in _FUNCS:
            try:
                return _check(_FUNCS[name](*args))
            except (TypeError, ValueError, OverflowError) as exc:
                raise CalcError(f"{name}: {exc}") from exc
        raise CalcError(f"unknown function {name!r}")
    raise CalcError("only numbers, operators and math functions are allowed")


def calculate(expression: str):
    text = str(expression or "").strip().replace("^", "**").replace("×", "*").replace("÷", "/")
    if not text:
        raise CalcError("empty expression")
    if len(text) > _MAX_EXPR_CHARS:
        raise CalcError(f"expression longer than {_MAX_EXPR_CHARS} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CalcError("not a valid expression") from exc
    return _eval(tree)


def _format(value) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


async def calculator_tool(content: str, ctx: dict | None = None) -> dict:
    raw = (content or "").strip()
    expressions: list[str]
    if raw.startswith("{"):
        try:
            args = json.loads(raw)
        except (TypeError, ValueError):
            return {"error": "calculate: arguments are not valid JSON", "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "calculate expects {\"expression\": ...}", "exit_code": 1}
        expressions = []
        if isinstance(args.get("expressions"), list):
            expressions = [str(x) for x in args["expressions"][:20]]
        if args.get("expression"):
            expressions.insert(0, str(args["expression"]))
    else:
        expressions = [line for line in raw.splitlines() if line.strip()][:20]
    if not expressions:
        return {"error": "calculate needs an expression", "exit_code": 1}
    lines, failed = [], 0
    for expr in expressions:
        try:
            lines.append(f"{expr.strip()} = {_format(calculate(expr))}")
        except CalcError as exc:
            failed += 1
            lines.append(f"{expr.strip()} → error: {exc}")
    result = {"output": "\n".join(lines), "exit_code": 0 if failed < len(expressions) else 1}
    if failed == len(expressions):
        result["error"] = lines[0].split("→ error: ", 1)[-1]
    return result


def calculator_available(owner: Any = None, session_id: Any = None) -> bool:
    """The calculator is offered while auto mode is on for the chat."""
    return chat_enabled(owner, session_id)


def with_calculator_grant(authority, *, session_id: Any = None):
    """Request authority with a ``calculate`` grant added (auto mode only)."""
    if authority is None or not calculator_available(getattr(authority, "owner", None), session_id):
        return authority
    from src.agent_runtime.authority import OperationGrant, canonical_tool

    tool = canonical_tool(CALCULATOR_TOOL)
    if tool in getattr(authority, "denied", frozenset()) or getattr(authority, "block_all", False):
        return authority
    if any(g.tool == tool for g in getattr(authority, "grants", ()) or ()):
        return authority
    from src.agent_runtime.remote_resources import resolve_backend
    try:
        backend = resolve_backend(tool, admission=True, owner=authority.owner)
    except (ValueError, TypeError, AttributeError):
        return authority
    backends = authority.backend_resources
    if backends is not None and backend not in backends:
        backends = (*backends, backend)
    return replace(authority, grants=(*authority.grants, OperationGrant(tool)),
                   backend_resources=backends)


def with_calculator_offer(contract, *, owner: Any = None, session_id: Any = None):
    """Turn contract that also offers ``calculate`` when it offers any tool."""
    if contract is None or not calculator_available(owner, session_id):
        return contract
    if CALCULATOR_TOOL in contract.offered or not contract.offered:
        return contract
    if contract.required_read_operation is not None or contract.unavailable:
        return contract
    schema = json.dumps(CALCULATOR_SCHEMA, sort_keys=True)
    return replace(
        contract,
        offered=frozenset(contract.offered | {CALCULATOR_TOOL}),
        executable=frozenset(contract.executable | {CALCULATOR_TOOL}),
        schema_json=tuple(sorted((*contract.schema_json, schema))),
    )
