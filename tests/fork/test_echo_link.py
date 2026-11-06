"""Fork: "Link this Echo" (fork/voice/echo_link.py): the Echo's return URL, who may
approve, the voice-only token and the page that hands it to the Echo. Fictional data."""
from __future__ import annotations

import html
import re
import sys
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fork.voice import echo_link
from fork.voice.routes import PREFIX

CODE = "Ab3_dE-fGh1jKl2mNo"  # 18 characters
RET = f"http://192.168.1.40:10801/setup/{CODE}/odysseus"
TS_RET = f"http://kitchen-echo.tail1234.ts.net:10801/setup/{CODE}/odysseus"


# --------------------------------------------------------------------------- return URL
@pytest.mark.parametrize("host, tailscale", [
    ("10.0.0.5", False),
    ("172.16.0.9", False),
    ("172.31.255.254", False),
    ("192.168.1.40", False),
    ("100.64.0.1", True),
    ("100.101.7.33", True),
    ("100.127.255.254", True),
    ("kitchen-echo.tail1234.ts.net", True),
    ("Kitchen-Echo.Tail1234.TS.NET", True),
    ("echo-show.local", False),
    ("echo.lan", False),
    ("echo.home.arpa", False),
    ("echoshow", False),
])
def test_accepts_the_addresses_an_echo_uses(host, tailscale):
    target = echo_link.check_return(f"http://{host}:10801/setup/{CODE}/odysseus")
    assert target.host == host.lower() and target.tailscale is tailscale
    assert target.origin == f"http://{host.lower()}:10801"
    assert target.url == f"{target.origin}/setup/{CODE}/odysseus"


@pytest.mark.parametrize("url", [
    f"https://192.168.1.40:10801/setup/{CODE}/odysseus",  # https: the Echo serves http
    f"HTTP://192.168.1.40:10801/setup/{CODE}/odysseus",
    f"http://192.168.1.40/setup/{CODE}/odysseus",  # no port
    f"http://192.168.1.40:10800/setup/{CODE}/odysseus",  # other port
    f"http://192.168.1.40:010801/setup/{CODE}/odysseus",
    f"http://192.168.1.40:10801/setup/{CODE}/odysseus/",
    f"http://192.168.1.40:10801/setup/{CODE}",
    f"http://192.168.1.40:10801/setup/{CODE}/ha",
    "http://192.168.1.40:10801/setup/short/odysseus",  # code < 16
    f"http://192.168.1.40:10801/setup/{'a' * 65}/odysseus",  # code > 64
    "http://192.168.1.40:10801/setup/abcdefghijklmnop.q/odysseus",
    f"http://192.168.1.40:10801/setup/{CODE}/../odysseus",
    f"http://192.168.1.40:10801/setup/{CODE}/odysseus?x=1",  # query
    f"http://192.168.1.40:10801/setup/{CODE}/odysseus#x",  # fragment
    f"http://user@192.168.1.40:10801/setup/{CODE}/odysseus",  # user info
    f"http://user:pw@192.168.1.40:10801/setup/{CODE}/odysseus",
    f"http://evil.example@192.168.1.40:10801/setup/{CODE}/odysseus",
    f"http://192.168.1.40:10801\\@evil.example/setup/{CODE}/odysseus",
    f" http://192.168.1.40:10801/setup/{CODE}/odysseus",
    f"http://192.168.1.40:10801/setup/{CODE}/odysseus\n",
    f"//192.168.1.40:10801/setup/{CODE}/odysseus",
    f"http://8.8.8.8:10801/setup/{CODE}/odysseus",  # public IP
    f"http://100.128.0.1:10801/setup/{CODE}/odysseus",  # just past 100.64/10
    f"http://172.32.0.1:10801/setup/{CODE}/odysseus",  # just past 172.16/12
    f"http://127.0.0.1:10801/setup/{CODE}/odysseus",  # loopback: the phone itself
    f"http://169.254.1.1:10801/setup/{CODE}/odysseus",
    f"http://0.0.0.0:10801/setup/{CODE}/odysseus",
    f"http://192.168.001.040:10801/setup/{CODE}/odysseus",  # leading zeros
    f"http://10.1:10801/setup/{CODE}/odysseus",  # a browser reads it as 10.0.0.1
    f"http://0x0a.0.0.1:10801/setup/{CODE}/odysseus",
    f"http://192.168.1.40.:10801/setup/{CODE}/odysseus",
    f"http://localhost:10801/setup/{CODE}/odysseus",
    f"http://echo.localhost:10801/setup/{CODE}/odysseus",
    f"http://evil.example.com:10801/setup/{CODE}/odysseus",  # public domain
    f"http://echo.ts.net.evil.com:10801/setup/{CODE}/odysseus",
    f"http://ts.net:10801/setup/{CODE}/odysseus",
    f"http://.local:10801/setup/{CODE}/odysseus",
    f"http://echo..lan:10801/setup/{CODE}/odysseus",
    f"http://-echo.lan:10801/setup/{CODE}/odysseus",
    f"http://[fd00::1]:10801/setup/{CODE}/odysseus",
    "",
])
def test_refuses_anything_else(url):
    with pytest.raises(echo_link.LinkError) as err:
        echo_link.check_return(url)
    assert err.value.status == 400


def test_names_are_cleaned_and_cut():
    assert echo_link.clean_name("  Kitchen\n\tShow ") == "Kitchen Show"
    assert len(echo_link.clean_name("x" * 60)) == 40
    assert echo_link.token_name("Kitchen", date(2026, 11, 3)) == "Echo Kitchen 2026-11-03"
    assert echo_link.token_name("", date(2026, 11, 3)) == "Echo 2026-11-03"


@pytest.mark.parametrize("url, origin", [
    ("http://homeassistant.local:8123", "http://homeassistant.local:8123"),
    ("https://ha.tail1234.ts.net/", "https://ha.tail1234.ts.net"),
    ("http://[fd00::5]:8123", "http://[fd00::5]:8123"),
    ("ftp://ha.lan", ""),
    ("http://u:p@ha.lan:8123", ""),
    ("http://ha.lan:8123/?x=1", ""),
    ("http://ha.lan;script-src *", ""),
    ("http://ha.lan:99999", ""),
    ("", ""),
])
def test_ha_origin(url, origin):
    assert echo_link.ha_origin(url) == origin


# ------------------------------------------------------------------------------ routes
@pytest.fixture
def echo(monkeypatch):
    """The router on a bare app. Header ``x-test-user`` signs in (``x-test-admin: 0``
    makes a non-admin), ``x-test-scopes`` makes it an API token; tokens are faked."""
    app = FastAPI()
    made = []
    invalidated = []
    app.state.invalidate_token_cache = lambda: invalidated.append(1)
    admins = {"alice"}
    app.state.auth_manager = SimpleNamespace(is_configured=True,
                                             is_admin=lambda user: user in admins)

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        user = request.headers.get("x-test-user")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = "alice" if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        request.state.current_user = "api" if scopes is not None else user
        return await call_next(request)

    monkeypatch.setattr("core.middleware.auth_disabled", lambda: False)
    monkeypatch.setattr("fork.athena.phone_pairing.create_api_token",
                        lambda owner, name, scopes: made.append((owner, name, scopes))
                        or {"id": "t1", "token": "ody_fake<&\"'>"})
    pruned = []  # the old-token cleanup, without a database (its own tests are below)
    monkeypatch.setattr(echo_link, "prune_echo_tokens",
                        lambda owner, name, keep: pruned.append((owner, name, keep)) or 0)
    marked = []  # the Echo-token marks, without a data dir (their own tests are below)
    monkeypatch.setattr(echo_link, "mark_echo_token",
                        lambda owner, token_id: marked.append((owner, token_id)))
    monkeypatch.setitem(sys.modules, "fork.voice.ha_settings",
                        SimpleNamespace(home_assistant_url=lambda: "http://homeassistant.local:8123/"))
    monkeypatch.setattr(echo_link, "APPROVALS", echo_link._Approvals())
    app.include_router(echo_link.setup_echo_link_routes())
    client = TestClient(app, base_url="https://odysseus.tail1234.ts.net")
    client.made, client.invalidated, client.pruned = made, invalidated, pruned
    client.marked = marked
    return client


ALICE = {"x-test-user": "alice"}
SAME = {**ALICE, "sec-fetch-site": "same-origin", "origin": "https://odysseus.tail1234.ts.net"}
FORM = {"return": RET, "name": "Kitchen"}


def _get(client, ret=RET, name="Kitchen", headers=ALICE):
    return client.get(echo_link.PATH, params={"return": ret, "name": name}, headers=headers)


def _form_fields(text: str) -> dict[str, str]:
    return {html.unescape(k): html.unescape(v) for k, v in
            re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)">', text)}


def test_the_page_shows_the_echo_and_where_the_token_goes(echo):
    res = _get(echo, name="Kitchen <b>")
    assert res.status_code == 200
    assert "Kitchen &lt;b&gt;" in res.text and "Kitchen <b>" not in res.text
    assert "192.168.1.40:10801" in res.text
    assert "unencrypted" in res.text  # plain Wi-Fi note
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["referrer-policy"] == "no-referrer"
    csp = res.headers["content-security-policy"]
    assert "form-action 'self';" in csp and "frame-ancestors 'none'" in csp
    assert _form_fields(res.text) == {"return": RET, "name": "Kitchen <b>"}
    assert not echo.made  # a GET never makes a token


def test_over_tailscale_there_is_no_wifi_note(echo):
    res = _get(echo, ret=TS_RET)
    assert res.status_code == 200 and "unencrypted" not in res.text
    assert "kitchen-echo.tail1234.ts.net:10801" in res.text


def test_a_bad_return_url_is_a_400_page(echo):
    res = _get(echo, ret="http://evil.example.com:10801/setup/" + CODE + "/odysseus")
    assert res.status_code == 400 and "Approve" not in res.text


@pytest.mark.parametrize("headers", [
    {},  # nobody signed in (the real app sends them to /login first)
    {"x-test-user": "bob"},  # signed in, not an admin
    {"x-test-scopes": "voice"},
    {"x-test-scopes": "chat,voice,todos:write"},
])
def test_only_a_signed_in_admin_sees_or_approves(echo, headers):
    assert _get(echo, headers=headers).status_code == 403
    res = echo.post(echo_link.PATH, data=FORM,
                    headers={**headers, "sec-fetch-site": "same-origin"})
    assert res.status_code == 403 and not echo.made


@pytest.mark.parametrize("headers", [
    {**ALICE, "sec-fetch-site": "cross-site"},
    {**ALICE, "sec-fetch-site": "same-site", "origin": "https://ha.tail1234.ts.net"},
    {**ALICE, "origin": "https://evil.example"},  # no Sec-Fetch-Site, foreign Origin
    ALICE,  # neither header
])
def test_approve_must_come_from_this_page(echo, headers):
    res = echo.post(echo_link.PATH, data=FORM, headers=headers)
    assert res.status_code == 403 and not echo.made


def _handoff(text: str) -> tuple[str, dict[str, str]]:
    """The Continue link's URL before ``#`` and its fragment's fields."""
    href = html.unescape(re.search(r'<a id="echo-handoff"[^>]* href="([^"]*)"', text).group(1))
    url, _, fragment = href.partition("#")
    return url, dict(parse_qsl(fragment, keep_blank_values=True))


def test_approve_makes_a_voice_and_recording_token_and_hands_it_to_the_echo(echo):
    res = echo.post(echo_link.PATH, data=FORM, headers=SAME)
    assert res.status_code == 200
    owner, name, scopes = echo.made[0]
    assert owner == "alice" and scopes == ["voice", "athena:ingest"]  # nothing else
    assert name == f"Echo Kitchen {date.today().isoformat()}"
    assert echo.marked == [("alice", "t1")]  # remembered as an Echo token
    assert echo.pruned == [("alice", "Kitchen", "t1")]  # this Echo's old tokens go
    assert echo.invalidated
    url, fields = _handoff(res.text)
    assert url == RET  # the token rides only in the fragment
    assert fields == {
        "token": "ody_fake<&\"'>",
        "base_url": "https://odysseus.tail1234.ts.net" + PREFIX,
        "ha_url": "http://homeassistant.local:8123",
    }
    assert "ody_fake<" not in res.text  # encoded and escaped
    assert "<form" not in res.text  # a navigation, not a form post: no insecure-form warning
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["referrer-policy"] == "no-referrer"
    csp = res.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
    nonce = re.search(r"'nonce-([0-9a-f]+)'", csp).group(1)
    script = re.search(rf'<script nonce="{nonce}">([^<]*)</script>', res.text).group(1)
    # the script only follows the Continue link; no value is written into script text
    assert script == "location.replace(document.getElementById('echo-handoff').href);"


def test_the_origin_without_fetch_metadata_must_be_this_host(echo):
    headers = {**ALICE, "origin": "https://odysseus.tail1234.ts.net"}
    res = echo.post(echo_link.PATH, data=FORM, headers=headers)
    assert res.status_code == 200 and echo.made


def test_a_bad_return_url_makes_no_token(echo):
    res = echo.post(echo_link.PATH, data={**FORM, "return": RET + "?x"}, headers=SAME)
    assert res.status_code == 400 and not echo.made


@pytest.mark.parametrize("saved", ["", "not a url", "javascript:alert(1)"])
def test_no_or_a_bad_ha_address_is_sent_empty(echo, monkeypatch, saved):
    monkeypatch.setitem(sys.modules, "fork.voice.ha_settings",
                        SimpleNamespace(home_assistant_url=lambda: saved))
    res = echo.post(echo_link.PATH, data=FORM, headers=SAME)
    assert _handoff(res.text)[1]["ha_url"] == ""


def test_without_the_ha_settings_module_the_address_is_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "fork.voice.ha_settings", None)  # import fails
    assert echo_link.saved_ha_url() == ""


def test_approves_are_rate_limited(echo):
    codes = [echo.post(echo_link.PATH, data=FORM, headers=SAME).status_code
             for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    assert len(echo.made) == 10


def test_no_token_in_the_log(echo, caplog):
    caplog.set_level("DEBUG")
    echo.post(echo_link.PATH, data=FORM, headers=SAME)
    assert "ody_fake" not in caplog.text and "Echo Kitchen" in caplog.text


def test_the_page_says_what_the_token_can_do(echo):
    text = _get(echo).text
    assert "ask the voice assistant" in text and "recordings and voice notes" in text


def test_the_scopes_are_known_to_the_relay():
    from fork.athena.urls import INGEST
    from fork.voice.routes import SCOPE

    assert echo_link.SCOPES == [SCOPE, "athena:ingest"] and "athena:ingest" in INGEST


def test_a_failed_mark_still_links(echo, monkeypatch):
    def broken(owner, token_id):
        raise OSError("read-only")

    monkeypatch.setattr(echo_link, "mark_echo_token", broken)
    res = echo.post(echo_link.PATH, data=FORM, headers=SAME)
    assert res.status_code == 200 and _handoff(res.text)[1]["token"]


@pytest.fixture
def tokens(monkeypatch, data_dir):
    """An in-memory API token table, as Settings → API tokens keeps it."""
    import contextlib
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.database import ApiToken

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    ApiToken.__table__.create(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def session():
        db = Session()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    monkeypatch.setattr("core.database.get_db_session", session)

    def add(id_, name, day, owner="alice", scopes="voice", used=None):
        with session() as db:
            db.add(ApiToken(id=id_, owner=owner, name=name, token_hash="x",
                            token_prefix="ody_x", scopes=scopes, is_active=True,
                            created_at=datetime(2026, 11, day),
                            last_used_at=datetime(2026, 11, used) if used else None))

    def ids():
        with session() as db:
            return sorted(t.id for t in db.query(ApiToken).all())

    return SimpleNamespace(add=add, ids=ids)


def test_relinking_an_echo_keeps_the_new_and_the_previous_token_only(tokens):
    tokens.add("k1", "Echo Kitchen 2026-11-01", 1)
    tokens.add("k2", "Echo Kitchen 2026-11-02", 2, used=3)  # the one the Echo uses now
    tokens.add("k3", "Echo Kitchen 2026-11-02", 2)
    tokens.add("new", "Echo Kitchen 2026-11-04", 4)
    tokens.add("b1", "Echo Bedroom 2026-11-01", 1)  # another Echo
    tokens.add("x1", "Echo Kitchen 2026-11-01", 1, scopes="voice,chat")  # not ours
    tokens.add("m1", "Echo Kitchen 2026-11-01", 1, owner="bob")
    tokens.add("h1", "Echo Kitchen extra 2026-11-01", 1)
    assert echo_link.prune_echo_tokens("alice", "Kitchen", "new") == 2
    assert tokens.ids() == ["b1", "h1", "k2", "m1", "new", "x1"]


def test_unnamed_echos_keep_their_tokens(tokens):
    tokens.add("u1", "Echo 2026-11-01", 1)
    tokens.add("u2", "Echo 2026-11-02", 2)
    assert echo_link.prune_echo_tokens("alice", "", "u3") == 0
    assert tokens.ids() == ["u1", "u2"]


def test_relinking_prunes_old_voice_only_and_new_marked_tokens(tokens):
    tokens.add("v1", "Echo Kitchen 2026-11-01", 1)  # linked before recordings: voice only
    tokens.add("n2", "Echo Kitchen 2026-11-02", 2, scopes="voice,athena:ingest")
    tokens.add("n3", "Echo Kitchen 2026-11-03", 3, scopes="voice,athena:ingest")
    tokens.add("new", "Echo Kitchen 2026-11-04", 4, scopes="voice,athena:ingest")
    for token_id in ("n2", "n3", "new"):
        echo_link.mark_echo_token("alice", token_id)
    assert echo_link.prune_echo_tokens("alice", "Kitchen", "new") == 2
    assert tokens.ids() == ["n3", "new"]  # the new one and the previous one
    assert echo_link.marked_echo_tokens("alice") == {"n3", "new"}  # deleted ones forgotten


def test_relinking_never_prunes_tokens_it_did_not_make(tokens):
    tokens.add("p1", "Echo Kitchen 2026-11-01", 1, scopes="voice,athena:ingest")  # unmarked
    tokens.add("p2", "Echo Kitchen 2026-11-01", 1, scopes="athena:ingest")  # no voice
    tokens.add("p3", "Echo Kitchen 2026-11-01", 1, scopes="voice,athena:ingest,chat")
    tokens.add("p4", "Phone 2026-11-01", 1, scopes="voice,athena:ingest")
    tokens.add("p5", "Echo Kitchen 2026-11-01", 1, owner="bob", scopes="voice,athena:ingest")
    for token_id in ("p3", "p4", "p5"):
        echo_link.mark_echo_token("alice", token_id)  # even marked: wrong scopes or name
    tokens.add("k1", "Echo Kitchen 2026-11-02", 2)
    tokens.add("new", "Echo Kitchen 2026-11-04", 4, scopes="voice,athena:ingest")
    assert echo_link.prune_echo_tokens("alice", "Kitchen", "new") == 0  # k1 is kept
    assert tokens.ids() == ["k1", "new", "p1", "p2", "p3", "p4", "p5"]


def test_marks_are_kept_per_owner(data_dir):
    echo_link.mark_echo_token("alice", "a1")
    echo_link.mark_echo_token("alice", "a2")
    echo_link.mark_echo_token("bob", "b1")
    assert echo_link.marked_echo_tokens("alice") == {"a1", "a2"}
    assert echo_link.marked_echo_tokens("bob") == {"b1"}
    assert echo_link.marked_echo_tokens("carol") == set()
    (data_dir / "echo_tokens_alice.json").write_text("not json")
    assert echo_link.marked_echo_tokens("alice") == set()
