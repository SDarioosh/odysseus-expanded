"""Sets up the sandbox Home Assistant for the barge-in sandbox (docs/fork/sandbox.md#voice-barge-in):
onboarding, the fake speech services, the Odysseus agent stand-in, the "Odysseus" Assist
pipeline (preferred) and the Echo emulator as a Wyoming satellite.

usage: ha_setup.py <odysseus voice token>   (the Echo emulator must be running)
Sandbox credentials only (owner / sandbox-ha-password); the tokens go to $BARGE_WORK.
"""
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp

B = "http://127.0.0.1:8123"
CID = B + "/"
WORK = Path(os.environ.get("BARGE_WORK", Path.home() / "sbx/barge"))
TOKENS = WORK / "ha_tokens.json"
ODYSSEUS = "http://127.0.0.1:7010/api/chat/voice/v1"


def req(method, path, data=None, token=None, form=False):
    headers, body = {}, None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(B + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r) as res:
            return json.loads(res.read() or b"null")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: {e.code} {e.read()[:300]!r}") from None


def onboard():
    r = req("POST", "/api/onboarding/users", {"client_id": CID, "name": "Owner", "username": "owner",
                                              "password": "sandbox-ha-password", "language": "en"})
    t = req("POST", "/auth/token", {"grant_type": "authorization_code", "code": r["auth_code"],
                                    "client_id": CID}, form=True)
    TOKENS.write_text(json.dumps(t))
    a = t["access_token"]
    req("POST", "/api/onboarding/core_config", {}, a)
    req("POST", "/api/onboarding/analytics", {}, a)
    req("POST", "/api/onboarding/integration", {"client_id": CID, "redirect_uri": CID + "?auth_callback=1"}, a)


def token():
    t = json.loads(TOKENS.read_text())
    return req("POST", "/auth/token", {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                       "client_id": CID}, form=True)["access_token"]


def flow(domain, data):
    a = token()
    f = req("POST", "/api/config/config_entries/flow", {"handler": domain}, a)
    r = req("POST", f"/api/config/config_entries/flow/{f['flow_id']}", data, a)
    print(domain, r.get("type"), r.get("title"))


async def pipeline():
    states = req("GET", "/api/states", token=token())
    agent = next(s["entity_id"] for s in states if s["entity_id"].startswith("conversation.odysseus_sbx"))
    async with aiohttp.ClientSession() as s, s.ws_connect(B + "/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token()})
        await ws.receive_json()
        await ws.send_json({"id": 1, "type": "assist_pipeline/pipeline/create", "name": "Odysseus",
                            "language": "en", "conversation_engine": agent, "conversation_language": "en",
                            "stt_engine": "stt.fake_stt", "stt_language": "en", "tts_engine": "tts.fake_tts",
                            "tts_language": "en", "tts_voice": None, "wake_word_entity": None,
                            "wake_word_id": None, "prefer_local_intents": False})
        pid = (await ws.receive_json())["result"]["id"]
        await ws.send_json({"id": 2, "type": "assist_pipeline/pipeline/set_preferred", "pipeline_id": pid})
        print("pipeline", (await ws.receive_json())["success"])


if __name__ == "__main__":
    onboard()
    flow("wyoming", {"host": "127.0.0.1", "port": 10300})
    flow("wyoming", {"host": "127.0.0.1", "port": 10200})
    flow("odysseus_sbx", {"base_url": ODYSSEUS, "token": sys.argv[1]})
    asyncio.run(pipeline())
    flow("wyoming", {"host": "127.0.0.1", "port": 10700})  # the Echo emulator
