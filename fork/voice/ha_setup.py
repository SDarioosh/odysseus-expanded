"""Fork: set up Home Assistant for Odysseus's voice endpoint (docs/fork/voice-assistant.md).

What the manual "Setup (owner)" steps do, in one run against Home Assistant's own API
(:func:`setup_voice`), with an access token of an HA administrator:

1. **Home Assistant**: the token works and belongs to an administrator.
2. **Speech**: the local speech-to-text and text-to-speech of the Wyoming integration
   (Whisper and Piper add-ons); nothing else is used (no cloud speech).
3. **Conversation agent**: an Extended OpenAI Conversation (3.x) entry named "Odysseus"
   with the voice token as key and ``<odysseus>/api/chat/voice/v1`` as base URL, and one
   conversation subentry "Odysseus" (model ``odysseus``, no tools, a one-line prompt:
   Odysseus ignores system prompts). That integration has no way to change an entry's
   key, so a rerun adds a new entry and then removes the older ones named "Odysseus".
4. **Assist pipeline** "Odysseus": that speech, that agent, language English, "prefer
   handling commands locally" on. Updated in place when it exists.
5. **Preferred assistant**: that pipeline.

Each step reports ``ok`` (already right), ``changed``, ``error`` or ``skipped``. The run
stops at the first error. Rerunning gives the same result.

:func:`run_setup` adds the Odysseus side: it makes the voice token ("Home Assistant
<date>", scope ``voice`` only), and once Home Assistant uses it, deletes the tokens of
earlier runs; a token Home Assistant never got is deleted again.

The browser flow (sign in to HA, then this) is ``fork/voice/ha_routes.py``; the same run
without a browser, with an HA long-lived token, is ``scripts/ha_voice_setup.py``.

HTTP calls go through ``httpx`` (REST), Assist pipelines and subentries through HA's
websocket API (the ``websockets`` package); tests replace both. Neither uses a proxy:
Home Assistant is on the owner's network. Tokens are never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode, urlsplit

logger = logging.getLogger(__name__)

DOMAIN = "extended_openai_conversation"
NAME = "Odysseus"
MODEL = "odysseus"  # fork.voice.openai_api.MODEL_ID
# EOC renders the prompt as a template and sends it as the system message, which the
# voice endpoint ignores (the voice instructions are Odysseus's own).
PROMPT = "You are Odysseus, a voice assistant. Answer briefly."
# EOC reads "functions" as YAML and falls back to its own default tools when the text is
# empty; "[]" is the way to say "no tools".
NO_TOOLS = "[]"
LANGUAGE = "en"
SCOPE = "voice"
TOKEN_PREFIX = "Home Assistant"
TOKEN_NAME_RE = re.compile(r"^Home Assistant \d{4}-\d{2}-\d{2}$")
TIMEOUT = 20.0

INSTALL_SPEECH = ("Install the Whisper and Piper add-ons in Home Assistant first (Settings → "
                  "Add-ons), add both under Settings → Devices & services (Wyoming "
                  "Protocol), then run this again.")
INSTALL_AGENT = ("Install Extended OpenAI Conversation in Home Assistant first (HACS → "
                 "Integrations), restart Home Assistant, then run this again.")


class HAError(Exception):
    """Something Home Assistant refused or doesn't have; ``str()`` is shown to the owner."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- addresses
def check_ha_url(raw: str) -> str:
    """``scheme://host[:port]`` of a Home Assistant address, or :class:`ValueError`.

    http or https, a host, no user, ``?query`` or ``#part``, no path (HA serves from /).
    """
    text = (raw or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 -- raises ValueError for a bad port
    except ValueError:
        raise ValueError("The Home Assistant address isn't a valid URL.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The Home Assistant address must start with http:// or https:// "
                         "and name a host.")
    if parts.username or parts.password or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("The Home Assistant address can't have a user, ?query or #part.")
    if parts.path not in ("", "/"):
        raise ValueError("The Home Assistant address is only scheme, host and port "
                         "(for example http://homeassistant.local:8123).")
    if len(text) > 300:
        raise ValueError("The Home Assistant address is too long.")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def voice_base_url(odysseus_url: str) -> str:
    """The OpenAI base URL HA is given: ``<odysseus>/api/chat/voice/v1``."""
    from fork.athena.phone_pairing import PairingError, check_url
    from fork.voice.routes import PREFIX

    try:
        url = check_url(odysseus_url)
    except PairingError as exc:
        raise ValueError(exc.message) from None
    return url if url.endswith(PREFIX) else url + PREFIX


def token_name(day: date | None = None) -> str:
    return f"{TOKEN_PREFIX} {(day or date.today()).isoformat()}"


# ------------------------------------------------------------------------------- OAuth
def authorize_url(ha_url: str, client_id: str, redirect_uri: str, state: str) -> str:
    """HA's login page; it sends the browser to ``redirect_uri?code=…&state=…``."""
    query = urlencode({"response_type": "code", "client_id": client_id,
                       "redirect_uri": redirect_uri, "state": state})
    return f"{check_ha_url(ha_url)}/auth/authorize?{query}"


def _client(http: Any) -> tuple[Any, bool]:
    if http is not None:
        return http, False
    import httpx

    return httpx.AsyncClient(timeout=TIMEOUT, trust_env=False, follow_redirects=False), True


async def exchange_code(ha_url: str, code: str, client_id: str, *, http: Any = None) -> dict:
    """The tokens for an authorization code (``access_token``, ``refresh_token``)."""
    client, own = _client(http)
    try:
        try:
            res = await client.post(f"{check_ha_url(ha_url)}/auth/token", data={
                "grant_type": "authorization_code", "code": code, "client_id": client_id})
        except Exception as exc:  # noqa: BLE001 -- shown as a step error
            raise HAError(f"Couldn't reach Home Assistant: {type(exc).__name__}") from None
        try:
            body = res.json()
        except ValueError:
            body = {}
        if res.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            detail = body.get("error_description") if isinstance(body, dict) else None
            raise HAError(f"Home Assistant didn't accept the sign-in ({detail or res.status_code}).",
                          res.status_code)
        return body
    finally:
        if own:
            await client.aclose()


async def revoke(ha_url: str, refresh_token: str, *, http: Any = None) -> bool:
    """Revoke a refresh token (and so its access tokens). Never raises."""
    if not refresh_token:
        return False
    client, own = _client(http)
    try:
        res = await client.post(f"{check_ha_url(ha_url)}/auth/revoke",
                                data={"token": refresh_token})
        return res.status_code == 200
    except Exception:
        logger.warning("voice: couldn't revoke the Home Assistant sign-in", exc_info=True)
        return False
    finally:
        if own:
            await client.aclose()


# ------------------------------------------------------------------------------ client
WsConnect = Callable[[str], Awaitable[Any]]


async def websockets_connect(url: str) -> Any:
    """A websocket to HA (``send``/``recv``/``close``), with the ``websockets`` package."""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        raise HAError("Odysseus needs the Python package 'websockets' (>= 15) for this; "
                      "install it (it is in requirements.txt) and restart.") from None
    return await connect(url, proxy=None, open_timeout=TIMEOUT, max_size=64 * 2 ** 20)


class HomeAssistant:
    """HA's REST and websocket APIs with one access token. Use ``async with``."""

    def __init__(self, url: str, token: str, *, http: Any = None,
                 ws_connect: WsConnect | None = None, timeout: float = TIMEOUT):
        self.url = check_ha_url(url)
        self._token = token
        self._http, self._own_http = _client(http)
        self._ws_connect = ws_connect or websockets_connect
        self._ws: Any = None
        self._id = 0
        self._timeout = timeout

    async def __aenter__(self) -> HomeAssistant:  # noqa: PYI034
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # closing a broken socket
                logger.debug("voice: closing Home Assistant's websocket failed", exc_info=True)
            self._ws = None
        if self._own_http:
            await self._http.aclose()

    async def rest(self, method: str, path: str, body: Any = None) -> Any:
        try:
            res = await self._http.request(
                method, self.url + path, json=body,
                headers={"Authorization": f"Bearer {self._token}"})
        except Exception as exc:  # noqa: BLE001
            raise HAError(f"Couldn't reach Home Assistant at {self.url} "
                          f"({type(exc).__name__}).") from None
        if res.status_code == 401:
            raise HAError("Home Assistant didn't accept the sign-in.", 401)
        if res.status_code == 403:
            raise HAError("This needs a Home Assistant administrator account.", 403)
        if res.status_code >= 400:
            text = (res.text or "").strip()[:200]
            raise HAError(f"Home Assistant answered {res.status_code} to {method} "
                          f"{path.split('?')[0]}: {text}", res.status_code)
        if not res.content:
            return None
        try:
            return res.json()
        except ValueError:
            return res.text

    async def _recv(self) -> dict:
        raw = await asyncio.wait_for(self._ws.recv(), self._timeout)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    async def _open(self) -> None:
        scheme = "wss" if self.url.startswith("https:") else "ws"
        url = scheme + self.url[self.url.index(":"):] + "/api/websocket"
        try:
            self._ws = await self._ws_connect(url)
        except HAError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HAError(f"Couldn't open Home Assistant's websocket at {url} "
                          f"({type(exc).__name__}).") from None
        hello = await self._recv()
        if hello.get("type") != "auth_required":
            raise HAError("Home Assistant's websocket didn't ask to sign in.")
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        answer = await self._recv()
        if answer.get("type") != "auth_ok":
            raise HAError("Home Assistant didn't accept the sign-in.", 401)

    async def ws(self, kind: str, **fields: Any) -> Any:
        """One websocket command; its ``result``, or :class:`HAError`."""
        if self._ws is None:
            await self._open()
        self._id += 1
        msg_id = self._id
        await self._ws.send(json.dumps({"id": msg_id, "type": kind, **fields}))
        while True:
            data = await self._recv()
            if data.get("id") != msg_id or data.get("type") != "result":
                continue
            if not data.get("success"):
                error = data.get("error") or {}
                if error.get("code") == "unauthorized":
                    raise HAError("This needs a Home Assistant administrator account.", 403)
                raise HAError(f"Home Assistant: {error.get('message') or 'command failed'} "
                              f"({kind}).")
            return data.get("result")


# ------------------------------------------------------------------------------ report
@dataclass
class Report:
    steps: list[dict[str, str]] = field(default_factory=list)
    agent_created: bool = False  # an entry with the new voice token is in HA
    agent_done: bool = False  # ... and older "Odysseus" entries are gone
    conversation_entity: str = ""
    pipeline_id: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s["status"] in ("ok", "changed") for s in self.steps)

    def add(self, step: str, status: str, detail: str = "") -> None:
        self.steps.append({"step": step, "status": status, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "steps": list(self.steps),
                "conversation_entity": self.conversation_entity,
                "pipeline_id": self.pipeline_id}


STEPS = ("Home Assistant", "Speech", "Conversation agent", "Assist pipeline",
         "Preferred assistant")


@dataclass
class Speech:
    stt: str
    stt_language: str
    tts: str
    tts_language: str
    tts_voices: list[str]


# ------------------------------------------------------------------------------- steps
async def _check_admin(ha: HomeAssistant) -> str:
    config = await ha.rest("GET", "/api/config")
    user = await ha.ws("auth/current_user")
    if not (isinstance(user, dict) and user.get("is_admin")):
        raise HAError("Sign in to Home Assistant with an administrator account.", 403)
    version = config.get("version") if isinstance(config, dict) else None
    return f"Home Assistant {version}" if version else "Home Assistant"


def _pick(entities: list[dict], prefer: str) -> str:
    entities = sorted(entities, key=lambda e: e["entity_id"])
    for e in entities:
        if prefer in e["entity_id"] or prefer in str(e.get("original_name") or "").lower():
            return e["entity_id"]
    return entities[0]["entity_id"]


def _languages(providers: Any, engine: str) -> list[str] | None:
    for p in (providers or {}).get("providers") or []:
        if p.get("engine_id") == engine:
            return list(p.get("supported_languages") or [])
    return None


async def _find_speech(ha: HomeAssistant) -> Speech:
    registry = await ha.ws("config/entity_registry/list") or []
    wyoming = [e for e in registry if isinstance(e, dict) and e.get("platform") == "wyoming"
               and not e.get("disabled_by")]
    stt = [e for e in wyoming if str(e.get("entity_id", "")).startswith("stt.")]
    tts = [e for e in wyoming if str(e.get("entity_id", "")).startswith("tts.")]
    if not stt or not tts:
        missing = " and ".join(n for n, found in (("speech-to-text (Whisper)", stt),
                                                    ("text-to-speech (Piper)", tts)) if not found)
        raise HAError(f"No local {missing} in Home Assistant. {INSTALL_SPEECH}")
    stt_id, tts_id = _pick(stt, "whisper"), _pick(tts, "piper")
    stt_langs = _languages(await ha.ws("stt/engine/list", language=LANGUAGE), stt_id)
    tts_langs = _languages(await ha.ws("tts/engine/list", language=LANGUAGE), tts_id)
    for engine, langs in ((stt_id, stt_langs), (tts_id, tts_langs)):
        if langs is None:
            raise HAError(f"{engine} isn't running in Home Assistant: check its add-on.")
        if not langs:
            raise HAError(f"{engine} has no English voice or model: choose one in its add-on.")
    voices = await ha.ws("tts/engine/voices", engine_id=tts_id, language=tts_langs[0])
    voice_ids = [v.get("voice_id") for v in ((voices or {}).get("voices") or [])
                 if isinstance(v, dict) and v.get("voice_id")]
    return Speech(stt_id, stt_langs[0], tts_id, tts_langs[0], voice_ids)


def _schema_names(flow: dict) -> set[str] | None:
    schema = flow.get("data_schema")
    if not isinstance(schema, list):
        return None
    return {f.get("name") for f in schema if isinstance(f, dict)}


def _only_known(flow: dict, values: dict[str, Any]) -> dict[str, Any]:
    names = _schema_names(flow)
    return values if names is None else {k: v for k, v in values.items() if k in names}


async def _entries(ha: HomeAssistant) -> list[dict]:
    entries = await ha.rest("GET", f"/api/config/config_entries/entry?domain={DOMAIN}")
    return [e for e in entries or [] if isinstance(e, dict) and e.get("domain", DOMAIN) == DOMAIN]


_AGENT_ERRORS = {
    "cannot_connect": "Home Assistant couldn't reach Odysseus at {base}: check the Odysseus "
                      "address (as Home Assistant reaches it).",
    "invalid_auth": "Odysseus didn't accept the voice token from Home Assistant.",
}


async def _create_entry(ha: HomeAssistant, base_url: str, voice_token: str,
                        validate: bool) -> str:
    try:
        flow = await ha.rest("POST", "/api/config/config_entries/flow",
                             {"handler": DOMAIN, "show_advanced_options": False})
    except HAError as exc:
        if exc.status == 404:  # "Invalid handler specified"
            raise HAError(INSTALL_AGENT, exc.status) from None
        if exc.status == 400:  # "Failed dependencies …"
            raise HAError(f"Extended OpenAI Conversation can't start in Home Assistant "
                          f"({str(exc).split(': ', 1)[-1]}): see Home Assistant's log.",
                          exc.status) from None
        raise
    if flow.get("type") != "form":
        raise HAError(f"Extended OpenAI Conversation didn't offer its form "
                      f"({flow.get('reason') or flow.get('type')}).")
    flow_id = flow["flow_id"]
    values = _only_known(flow, {
        "name": NAME, "api_key": voice_token, "base_url": base_url,
        "skip_authentication": not validate, "follow_redirects": False,
        "api_provider": "openai"})
    result = await ha.rest("POST", f"/api/config/config_entries/flow/{flow_id}", values)
    if result.get("type") == "create_entry":
        return result["result"]["entry_id"]
    try:
        await ha.rest("DELETE", f"/api/config/config_entries/flow/{flow_id}")
    except HAError:
        pass
    error = (result.get("errors") or {}).get("base") or result.get("reason") or "unknown"
    template = _AGENT_ERRORS.get(error, "Extended OpenAI Conversation refused the setup ({error}).")
    raise HAError(template.format(base=base_url, error=error))


async def _wait_loaded(ha: HomeAssistant, entry_id: str, polls: int, pause: float) -> None:
    state = "?"
    for _ in range(max(1, polls)):
        entry = next((e for e in await _entries(ha) if e.get("entry_id") == entry_id), None)
        state = (entry or {}).get("state", "?")
        if state == "loaded":
            return
        if state in ("setup_error", "migration_error", "failed_unload"):
            break
        await asyncio.sleep(pause)
    raise HAError(f"The Odysseus agent was added but didn't start in Home Assistant "
                  f"(state {state}): check that Home Assistant reaches Odysseus.")


async def _add_subentry(ha: HomeAssistant, entry_id: str) -> str:
    before = {s["subentry_id"] for s in
              await ha.ws("config_entries/subentries/list", entry_id=entry_id) or []}
    flow = await ha.rest("POST", "/api/config/config_entries/subentries/flow",
                         {"handler": [entry_id, "conversation"], "show_advanced_options": False})
    if flow.get("type") != "form":
        raise HAError(f"Extended OpenAI Conversation didn't offer the agent's options "
                      f"({flow.get('reason') or flow.get('type')}).")
    values = _only_known(flow, {
        "name": NAME, "prompt": PROMPT, "chat_model": MODEL, "skills": [],
        "functions": NO_TOOLS, "advanced_options": False})
    result = await ha.rest("POST", f"/api/config/config_entries/subentries/flow/{flow['flow_id']}",
                           values)
    if result.get("type") != "create_entry":
        raise HAError(f"Extended OpenAI Conversation refused the agent's options "
                      f"({result.get('errors') or result.get('reason') or result.get('type')}).")
    after = await ha.ws("config_entries/subentries/list", entry_id=entry_id) or []
    new = [s for s in after if s["subentry_id"] not in before
           and s.get("subentry_type") == "conversation"]
    if not new:
        raise HAError("Home Assistant didn't list the new Odysseus agent.")
    keep = new[0]["subentry_id"]
    for sub in after:  # the defaults the entry was made with (a GPT agent and AI task)
        if sub["subentry_id"] != keep:
            await ha.ws("config_entries/subentries/delete", entry_id=entry_id,
                        subentry_id=sub["subentry_id"])
    return keep


async def _agent_entity(ha: HomeAssistant, entry_id: str, subentry_id: str,
                        polls: int, pause: float) -> str:
    for _ in range(max(1, polls)):
        registry = await ha.ws("config/entity_registry/list") or []
        for e in registry:
            if (isinstance(e, dict) and e.get("config_entry_id") == entry_id
                    and e.get("config_subentry_id") == subentry_id
                    and str(e.get("entity_id", "")).startswith("conversation.")):
                return e["entity_id"]
        await asyncio.sleep(pause)
    raise HAError("Home Assistant didn't make the Odysseus agent's entity.")


async def _tidy_entity_id(ha: HomeAssistant, entity_id: str) -> str:
    """``conversation.odysseus`` when that id is free (a rerun's agent is made while the
    old one still has it)."""
    wanted = "conversation." + NAME.lower()
    if entity_id == wanted:
        return entity_id
    registry = await ha.ws("config/entity_registry/list") or []
    if any(isinstance(e, dict) and e.get("entity_id") == wanted for e in registry):
        return entity_id
    try:
        await ha.ws("config/entity_registry/update", entity_id=entity_id, new_entity_id=wanted)
    except HAError:
        return entity_id
    return wanted


async def _agent(ha: HomeAssistant, report: Report, base_url: str, voice_token: str,
                 validate: bool, polls: int, pause: float) -> str:
    old = [e["entry_id"] for e in await _entries(ha)
           if str(e.get("title") or "").strip().casefold() == NAME.casefold()]
    entry_id = await _create_entry(ha, base_url, voice_token, validate)
    report.agent_created = True
    await _wait_loaded(ha, entry_id, polls, pause)
    subentry_id = await _add_subentry(ha, entry_id)
    for old_id in old:
        await ha.rest("DELETE", f"/api/config/config_entries/entry/{old_id}")
    report.agent_done = True
    entity = await _agent_entity(ha, entry_id, subentry_id, polls, pause)
    entity = await _tidy_entity_id(ha, entity)
    report.conversation_entity = entity
    replaced = f", replaced {len(old)} older" if old else ""
    return f"{entity} → {base_url} (model {MODEL}, no tools){replaced}"


def _pipeline_fields(entity: str, speech: Speech, existing: dict | None) -> dict[str, Any]:
    existing = existing or {}
    voice = existing.get("tts_voice")
    if (existing.get("tts_engine") != speech.tts or existing.get("tts_language") != speech.tts_language
            or voice not in speech.tts_voices):
        voice = speech.tts_voices[0] if speech.tts_voices else None
    fields: dict[str, Any] = {
        "name": NAME, "language": LANGUAGE,
        # EOC answers in any language (MATCH_ALL), which HA's pipelines write as "*".
        "conversation_engine": entity, "conversation_language": "*",
        "stt_engine": speech.stt, "stt_language": speech.stt_language,
        "tts_engine": speech.tts, "tts_language": speech.tts_language, "tts_voice": voice,
        # the satellite listens for its own wake word; keep one set in HA by hand
        "wake_word_entity": existing.get("wake_word_entity"),
        "wake_word_id": existing.get("wake_word_id"),
        "prefer_local_intents": True,
    }
    if existing.get("acknowledge_media_id"):
        fields["acknowledge_media_id"] = existing["acknowledge_media_id"]
    return fields


async def _pipeline(ha: HomeAssistant, report: Report, entity: str,
                    speech: Speech) -> tuple[str, str, dict]:
    listing = await ha.ws("assist_pipeline/pipeline/list") or {}
    pipelines = [p for p in listing.get("pipelines") or [] if isinstance(p, dict)]
    preferred = listing.get("preferred_pipeline")
    named = [p for p in pipelines if str(p.get("name", "")).strip().casefold() == NAME.casefold()]
    named.sort(key=lambda p: p.get("id") != preferred)
    existing = named[0] if named else None
    fields = _pipeline_fields(entity, speech, existing)
    if existing is None:
        created = await ha.ws("assist_pipeline/pipeline/create", **fields)
        report.pipeline_id = created["id"]
        status = "changed"
    else:
        report.pipeline_id = existing["id"]
        if any(existing.get(k) != v for k, v in fields.items()):
            await ha.ws("assist_pipeline/pipeline/update", pipeline_id=existing["id"], **fields)
            status = "changed"
        else:
            status = "ok"
    detail = (f"{speech.stt} → {entity} → {speech.tts}"
              f"{' (' + fields['tts_voice'] + ')' if fields['tts_voice'] else ''}, "
              "commands handled locally first")
    return status, detail, listing


async def setup_voice(ha_url: str, access_token: str, odysseus_base_url: str, voice_token: str,
                      *, validate: bool = True, http: Any = None,
                      ws_connect: WsConnect | None = None, polls: int = 20,
                      pause: float = 0.5) -> dict[str, Any]:
    """Set up HA for Odysseus's voice endpoint (module doc); a report for the UI.

    ``odysseus_base_url`` is Odysseus as HA reaches it (``https://odysseus.<tailnet>.ts.net``;
    the voice path is added). ``validate=False`` makes HA skip its check of the key (when
    the running Odysseus can't know the token yet, see ``scripts/ha_voice_setup.py``).
    """
    report = Report()
    try:
        base_url = voice_base_url(odysseus_base_url)
        ha = HomeAssistant(ha_url, access_token, http=http, ws_connect=ws_connect)
    except ValueError as exc:
        report.add(STEPS[0], "error", str(exc))
        return _finish(report)
    async with ha:
        speech: Speech | None = None
        for step in STEPS:
            try:
                if step == "Home Assistant":
                    report.add(step, "ok", await _check_admin(ha))
                elif step == "Speech":
                    speech = await _find_speech(ha)
                    report.add(step, "ok", f"{speech.stt} ({speech.stt_language}), "
                                           f"{speech.tts} ({speech.tts_language})")
                elif step == "Conversation agent":
                    report.add(step, "changed", await _agent(
                        ha, report, base_url, voice_token, validate, polls, pause))
                elif step == "Assist pipeline":
                    status, detail, listing = await _pipeline(
                        ha, report, report.conversation_entity, speech)
                    report.add(step, status, detail)
                elif step == "Preferred assistant":
                    if listing.get("preferred_pipeline") == report.pipeline_id:
                        report.add(step, "ok", f"{NAME} is the preferred assistant")
                    else:
                        await ha.ws("assist_pipeline/pipeline/set_preferred",
                                    pipeline_id=report.pipeline_id)
                        report.add(step, "changed", f"{NAME} is now the preferred assistant")
            except (TimeoutError, HAError, KeyError, TypeError, ValueError) as exc:
                message = str(exc) if isinstance(exc, HAError) else (
                    f"Unexpected answer from Home Assistant ({type(exc).__name__}).")
                logger.warning("voice: Home Assistant setup, %s: %s", step, message)
                report.add(step, "error", message)
                break
    return _finish(report)


def _finish(report: Report) -> dict[str, Any]:
    done = {s["step"] for s in report.steps}
    for step in STEPS:
        if step not in done:
            report.add(step, "skipped", "")
    out = report.as_dict()
    out["agent_created"] = report.agent_created
    out["agent_done"] = report.agent_done
    return out


# ------------------------------------------------------------------- the Odysseus side
def mint_token(owner: str) -> dict[str, str]:
    from fork.athena.phone_pairing import create_api_token

    name = token_name()
    token = create_api_token(owner, name, [SCOPE])
    return {**token, "name": name}


def delete_tokens(owner: str, *, only: str | None = None, keep: str | None = None) -> int:
    """Delete this feature's voice tokens of ``owner``: the one with id ``only``, or every
    "Home Assistant <date>" token with only the ``voice`` scope except ``keep``."""
    from core.database import ApiToken, get_db_session

    removed = 0
    with get_db_session() as db:
        rows = db.query(ApiToken).filter(ApiToken.owner == owner).all()
        for row in rows:
            if only is not None:
                hit = row.id == only
            else:
                hit = (row.id != keep and TOKEN_NAME_RE.match(row.name or "") is not None
                       and (row.scopes or "").strip() == SCOPE)
            if hit:
                db.delete(row)
                removed += 1
    return removed


async def run_setup(owner: str, ha_url: str, access_token: str, odysseus_url: str, *,
                    invalidate: Callable[[], None] | None = None, validate: bool = True,
                    **kwargs: Any) -> dict[str, Any]:
    """Make the voice token, run :func:`setup_voice`, then tidy the tokens.

    Old "Home Assistant <date>" tokens are deleted only once HA uses the new one and the
    entries that held them are gone; the new token is deleted if HA never got it.
    """
    def changed() -> None:
        if callable(invalidate):
            invalidate()

    token = await asyncio.to_thread(mint_token, owner)
    changed()
    try:
        report = await setup_voice(ha_url, access_token, odysseus_url, token["token"],
                                   validate=validate, **kwargs)
    except BaseException:
        await asyncio.to_thread(delete_tokens, owner, only=token["id"])
        changed()
        raise
    if not report["agent_created"]:
        await asyncio.to_thread(delete_tokens, owner, only=token["id"])
        report["token_name"] = ""
    else:
        report["token_name"] = token["name"]
        if report["agent_done"]:
            report["old_tokens_deleted"] = await asyncio.to_thread(
                delete_tokens, owner, keep=token["id"])
    changed()
    return report
