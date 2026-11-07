"""Fork: library images for the Echo's Music page (fork/voice/music_images.py, contract C8).

Fictional URLs (documentation address ranges, example.com) and pictures made here; a fake
fetcher and resolver, so nothing touches the network.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import os
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from fork.voice import music_images as mi
from fork.voice.routes import PREFIX

MA_URL = "http://192.168.1.20:8095"
MA = mi.music_assistant(MA_URL)
PROXY_ID = MA_URL + "/imageproxy/3f2a9c"
PROXY_QUERY = MA_URL + "/imageproxy?path=%2Fmusic%2Fcover.jpg&provider=filesystem_local"
CDN = "https://images.example.com/cover/ab12.jpg?w=640&token=secret-in-query"
PUBLIC_IP = "93.184.216.34"  # example.com's documented public address
VOICE = {"x-test-scopes": "voice"}


def picture(w=800, h=600, fmt="JPEG", mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (w, h), (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)).save(
        out, fmt)
    return out.getvalue()


class FakeNet:
    """Answers from a dict URL -> Fetched; records each (url, pinned address)."""

    def __init__(self, answers=None, dns=None):
        self.answers = answers or {}
        self.dns = dns or {}
        self.calls: list[tuple[str, str | None]] = []

    async def fetch(self, url, pin):
        self.calls.append((url, pin))
        got = self.answers.get(url)
        if isinstance(got, Exception):
            raise got
        return got or mi.Fetched(404)

    async def resolve(self, host):
        if host not in self.dns:
            raise OSError("no such host")
        return self.dns[host]


def run(coro):
    import asyncio

    return asyncio.run(coro)


def load(net, url, size=256, **kw):
    kw.setdefault("ma_url", MA_URL)
    kw.setdefault("internet", True)
    return run(mi.load(url, size, fetcher=net.fetch, resolver=net.resolve, **kw))


# ------------------------------------------------------------------------ the decision
@pytest.mark.parametrize("url", [PROXY_ID, PROXY_QUERY, PROXY_ID + "?size=256",
                                 MA_URL + "/imageproxy/spotify/abc%20def"])
def test_ma_imageproxy_both_forms_are_allowed(url):
    assert mi.decide(url, ma=MA, internet=False).kind == "ma"


@pytest.mark.parametrize("url", [
    "http://192.168.1.20:8123/imageproxy/3f2a9c",   # another port on MA's host
    "http://192.168.1.20/imageproxy/3f2a9c",
    "https://192.168.1.20:8095/imageproxy/3f2a9c",  # another scheme
    MA_URL + "/api",                                 # not the imageproxy
    MA_URL + "/imageproxy",                          # the query form needs a path
    MA_URL + "/imageproxy/",
    MA_URL + "/imageproxy/../api",
    MA_URL + "/imageproxy/%2e%2e/api",
    MA_URL + "/imageproxy/a%2Fb",
    MA_URL + "/imageproxyx/1",
    "http://192.168.1.21:8095/imageproxy/3f2a9c",   # another host
])
def test_other_urls_on_the_lan_are_refused(url):
    with pytest.raises(mi.NotAllowed):
        mi.decide(url, ma=MA, internet=True)


def ma_query(path, provider=None):
    from urllib.parse import urlencode

    params = {"path": path} | ({"provider": provider} if provider else {})
    return MA_URL + "/imageproxy?" + urlencode(params)


INNER = "https://images.example.com/cover/ab12.jpg"


@pytest.mark.parametrize("url", [
    # MA fetches an http(s) path itself: it must pass the internet rules on its own.
    ma_query("http://192.168.1.1/cgi-bin/x", "url"),
    ma_query("https://10.0.0.5/admin", "url"),
    ma_query("https://localhost/a.jpg", "url"),
    ma_query("http://images.example.com/a.jpg", "url"),   # not https
    ma_query("https://u:p@images.example.com/a.jpg", "url"),
    ma_query("http%3A%2F%2F10.0.0.5%2Fadmin", "url"),    # encoded once more
    # Other schemes, and builtin's local files.
    ma_query("file:///etc/passwd", "builtin"),
    ma_query("data:image/png;base64,AAAA", "builtin"),
    ma_query("ftp://images.example.com/a.jpg", "url"),
    ma_query("/data/secrets.png", "builtin"),
    ma_query("/data/secrets.png"),                        # no provider: builtin
    ma_query("../../etc/passwd", "filesystem_local"),
    ma_query("covers/../../x.jpg", "builtin"),
    ma_query("//10.0.0.5/share/a.jpg", "filesystem_smb"),
    ma_query(""),
    MA_URL + "/imageproxy?path=a.jpg&path=/etc/passwd&provider=builtin",
])
def test_ma_query_form_is_no_lan_proxy(url):
    for internet in (True, False):
        with pytest.raises(mi.NotAllowed):
            mi.decide(url, ma=MA, internet=internet)


def test_ma_query_form_with_a_public_https_path_needs_the_internet_setting():
    target = mi.decide(ma_query(INNER, "spotify"), ma=MA, internet=True)
    assert target.kind == "ma" and target.inner is not None
    assert (target.inner.kind, target.inner.url) == ("internet", INNER)
    with pytest.raises(mi.NotAllowed):
        mi.decide(ma_query(INNER, "spotify"), ma=MA, internet=False)
    assert mi.decide(ma_query("logo.png", "builtin"), ma=MA, internet=False).inner is None


def test_ma_base_path_and_host_case():
    ma = mi.music_assistant("http://MA.example.lan:8095/ma/")
    assert ma == mi.MusicAssistant("http", "ma.example.lan", 8095, "/ma")
    assert mi.decide("http://ma.example.lan:8095/ma/imageproxy/1", ma=ma,
                     internet=False).kind == "ma"
    with pytest.raises(mi.NotAllowed):
        mi.decide("http://ma.example.lan:8095/imageproxy/1", ma=ma, internet=True)


@pytest.mark.parametrize("raw", ["", "  ", "ftp://x", "http://u:p@192.168.1.20:8095",
                                 "http://192.168.1.20:8095?x=1", "192.168.1.20:8095",
                                 "http://192.168.1.20:99999"])
def test_a_bad_or_missing_ma_url_turns_ma_off(raw):
    assert mi.music_assistant(raw) is None
    with pytest.raises(mi.NotAllowed):
        mi.decide(PROXY_ID, ma=mi.music_assistant(raw), internet=True)


def test_https_is_allowed_only_with_the_setting_on():
    assert mi.decide(CDN, ma=MA, internet=True).kind == "internet"
    assert mi.decide(CDN, ma=None, internet=True).kind == "internet"  # no MA: still works
    with pytest.raises(mi.NotAllowed):
        mi.decide(CDN, ma=MA, internet=False)
    with pytest.raises(mi.NotAllowed):
        mi.decide(CDN.replace("https:", "http:"), ma=MA, internet=True)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/a.jpg", "https://10.0.0.5/a.jpg", "https://192.168.1.20:8095/a.jpg",
    "https://169.254.169.254/latest", "https://100.101.102.103/a.jpg",  # Tailscale
    "https://[::1]/a.jpg", "https://[fe80::1]/a.jpg", "https://[fd00::1]/a.jpg",
    "https://[::ffff:127.0.0.1]/a.jpg", "https://224.0.0.1/a.jpg", "https://0.0.0.0/a.jpg",
    "https://localhost/a.jpg", "https://nas.local/a.jpg", "https://nas/a.jpg",
    "https://router.home.arpa/a.jpg",
])
def test_private_and_loopback_addresses_are_refused(url):
    with pytest.raises(mi.NotAllowed):
        mi.decide(url, ma=MA, internet=True)


@pytest.mark.parametrize("url", ["", "not a url", "javascript:alert(1)", "file:///etc/passwd",
                                 "https://user:pw@images.example.com/a.jpg",
                                 "https://images.example.com:notaport/a.jpg",
                                 "https://" + "a" * 5000 + ".example.com/"])
def test_bad_urls(url):
    with pytest.raises(mi.BadUrl):
        mi.decide(url, ma=MA, internet=True)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1",
                                     "169.254.1.1", "100.64.0.1", "::1", "fe80::1",
                                     "fc00::1", "::ffff:10.0.0.1", "2002:7f00:1::1",
                                     "64:ff9b::7f00:1", "240.0.0.1", "0.0.0.0", "ff02::1"])
def test_addresses_that_are_not_public(address):
    assert not mi.public_address(address)


@pytest.mark.parametrize("address", [PUBLIC_IP, "2606:4700::1111"])
def test_public_addresses(address):
    assert mi.public_address(address)


def test_every_resolved_address_must_be_public():
    target = mi.decide(CDN, ma=MA, internet=True)
    assert mi.check_addresses(target, [PUBLIC_IP]) == PUBLIC_IP
    with pytest.raises(mi.NotAllowed):
        mi.check_addresses(target, [PUBLIC_IP, "192.168.1.20"])
    with pytest.raises(mi.NotFound):
        mi.check_addresses(target, [])
    assert mi.check_addresses(mi.decide(PROXY_ID, ma=MA, internet=False), []) is None


@pytest.mark.parametrize(("raw", "size"), [(None, 256), ("", 256), ("abc", 256), ("64", 64),
                                           ("10", 64), ("512", 512), ("4000", 512),
                                           (" 128 ", 128)])
def test_size(raw, size):
    assert mi.parse_size(raw) == size


# ----------------------------------------------------------------------- fetch and scale
def test_ma_image_is_fetched_by_name_and_scaled(data_dir):
    net = FakeNet({PROXY_ID: mi.Fetched(200, "image/jpeg", picture(800, 600))})
    data, ctype = load(net, PROXY_ID, 128, internet=False)
    assert ctype == "image/jpeg" and net.calls == [(PROXY_ID, None)]
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (128, 96)


def test_internet_image_is_pinned_to_the_checked_address(data_dir):
    net = FakeNet({CDN: mi.Fetched(200, "image/png", picture(300, 300, "PNG", "RGBA"))},
                  dns={"images.example.com": [PUBLIC_IP]})
    data, ctype = load(net, CDN)
    assert ctype == "image/png" and net.calls == [(CDN, PUBLIC_IP)]
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (256, 256) and img.mode == "RGBA"


def test_small_images_are_not_enlarged(data_dir):
    net = FakeNet({PROXY_ID: mi.Fetched(200, "image/webp", picture(50, 40, "WEBP"))})
    data, _ = load(net, PROXY_ID, 512)
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (50, 40)


def test_a_name_that_resolves_to_a_private_address_is_refused(data_dir):
    net = FakeNet(dns={"images.example.com": ["10.0.0.7"]})
    with pytest.raises(mi.NotAllowed):
        load(net, CDN)
    assert net.calls == []


def test_a_name_that_doesnt_resolve_is_not_found(data_dir):
    with pytest.raises(mi.NotFound):
        load(FakeNet(), CDN)


def test_redirects_are_checked_at_every_hop(data_dir):
    hop = "https://cdn2.example.com/x.jpg"
    net = FakeNet({CDN: mi.Fetched(302, location=hop),
                   hop: mi.Fetched(301, location="http://127.0.0.1/admin")},
                  dns={"images.example.com": [PUBLIC_IP], "cdn2.example.com": [PUBLIC_IP]})
    with pytest.raises(mi.NotAllowed):
        load(net, CDN)
    assert [u for u, _ in net.calls] == [CDN, hop]

    # A redirect to a name that resolves privately is refused too.
    net = FakeNet({CDN: mi.Fetched(302, location="https://evil.example.net/a.jpg")},
                  dns={"images.example.com": [PUBLIC_IP], "evil.example.net": ["192.168.1.1"]})
    with pytest.raises(mi.NotAllowed):
        load(net, CDN)
    assert len(net.calls) == 1


def test_ma_query_form_checks_the_inner_host_by_dns(data_dir):
    url = ma_query(INNER, "spotify")
    net = FakeNet({url: mi.Fetched(200, "image/jpeg", picture(100, 100))},
                  dns={"images.example.com": ["10.0.0.7"]})
    with pytest.raises(mi.NotAllowed):
        load(net, url)
    assert net.calls == []
    net.dns["images.example.com"] = [PUBLIC_IP]
    assert load(net, url)[1] == "image/jpeg"
    assert net.calls == [(url, None)]  # MA itself by name


def test_a_redirect_into_ma_is_refused(data_dir):
    for location in (PROXY_ID, ma_query("http://10.0.0.5/admin", "url")):
        net = FakeNet({CDN: mi.Fetched(302, location=location)},
                      dns={"images.example.com": [PUBLIC_IP]})
        with pytest.raises(mi.NotAllowed):
            load(net, CDN)
        assert [u for u, _ in net.calls] == [CDN]


def test_a_redirect_from_ma_is_an_internet_url(data_dir):
    net = FakeNet({PROXY_ID: mi.Fetched(302, location=CDN),
                   CDN: mi.Fetched(200, "image/jpeg", picture(100, 100))},
                  dns={"images.example.com": [PUBLIC_IP]})
    assert load(net, PROXY_ID)[1] == "image/jpeg"
    assert net.calls == [(PROXY_ID, None), (CDN, PUBLIC_IP)]
    with pytest.raises(mi.NotAllowed):
        load(net, PROXY_ID, 128, internet=False)  # another size: not cached
    net = FakeNet({PROXY_ID: mi.Fetched(302, location="/imageproxy/other")})
    with pytest.raises(mi.NotAllowed):
        load(net, PROXY_ID, 64)


def test_one_deadline_covers_dns_and_every_hop(data_dir, monkeypatch):
    monkeypatch.setattr(mi, "TOTAL_SECONDS", 0.2)

    class Hanging(FakeNet):
        async def resolve(self, host):
            await asyncio.Event().wait()  # DNS that never answers

    started = time.monotonic()
    with pytest.raises(mi.NotFound):
        load(Hanging(), CDN)
    assert time.monotonic() - started < 2


def test_scaling_runs_off_the_event_loop(data_dir, monkeypatch):
    threads = []
    real = mi.scale

    def spy(data, size):
        threads.append(threading.get_ident())
        return real(data, size)

    monkeypatch.setattr(mi, "scale", spy)
    load(FakeNet({PROXY_ID: mi.Fetched(200, "image/jpeg", picture(100, 100))}), PROXY_ID)
    assert threads and threads[0] != threading.get_ident()
    assert mi._SCALING._value == mi.SCALE_SLOTS == 2


def test_only_four_formats_are_read():
    with pytest.raises(mi.NotFound):
        mi.scale(picture(100, 100, "BMP"), 64)
    with pytest.raises(mi.NotFound):
        mi.scale(picture(100, 100, "TIFF"), 64)


def test_an_animated_gif_gives_its_first_frame():
    frames = [Image.new("RGB", (100, 100), c) for c in ((250, 0, 0), (0, 0, 250))]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
    data, ctype = mi.scale(out.getvalue(), 64)
    with Image.open(io.BytesIO(data)) as img:
        r, _g, b = img.convert("RGB").getpixel((32, 32))
    assert ctype == "image/jpeg" and r > 200 and b < 50


def test_a_relative_redirect_and_the_redirect_limit(data_dir):
    final = "https://images.example.com/cover/final.jpg"
    net = FakeNet({CDN: mi.Fetched(302, location="/cover/final.jpg"),
                   final: mi.Fetched(200, "image/jpeg", picture(100, 100))},
                  dns={"images.example.com": [PUBLIC_IP]})
    assert load(net, CDN)[1] == "image/jpeg"

    loop = {f"https://images.example.com/{i}": mi.Fetched(
        302, location=f"https://images.example.com/{i + 1}") for i in range(10)}
    net = FakeNet(loop, dns={"images.example.com": [PUBLIC_IP]})
    with pytest.raises(mi.NotFound):
        load(net, "https://images.example.com/0")
    assert len(net.calls) == mi.MAX_REDIRECTS + 1


@pytest.mark.parametrize("answer", [
    mi.Fetched(404), mi.Fetched(500), mi.Fetched(200, "text/html", b""),
    mi.Fetched(200, "image/jpeg", b"not really a picture"),
    mi.NotFound("too big"), httpx.ConnectTimeout("slow"), httpx.ConnectError("refused"),
])
def test_unusable_answers_are_not_found(data_dir, answer):
    with pytest.raises(mi.NotFound):
        load(FakeNet({PROXY_ID: answer}), PROXY_ID)
    assert not (data_dir / mi.CACHE_DIR).exists() or not any(
        (data_dir / mi.CACHE_DIR).iterdir())


def test_a_decompression_bomb_is_not_found(monkeypatch):
    monkeypatch.setattr(mi, "MAX_PIXELS", 100)
    with pytest.raises(mi.NotFound):
        mi.scale(picture(20, 20), 64)


def test_http_fetch_caps_the_size_and_does_not_follow_redirects(monkeypatch):
    big = b"\xff" * (mi.MAX_BYTES + 10)

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == "/big":
            return httpx.Response(200, headers={"content-type": "image/jpeg"},
                                  stream=httpx.ByteStream(big))
        if request.url.path == "/gzip":  # never decompressed, whatever its size
            return httpx.Response(200, headers={"content-type": "image/png",
                                                "content-encoding": "gzip"},
                                  stream=httpx.ByteStream(gzip.compress(b"\0" * 4096)))
        if request.url.path == "/move":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/"})
        assert request.headers["host"] == "images.example.com"
        assert request.url.host == PUBLIC_IP
        assert request.extensions["sni_hostname"] == "images.example.com"
        return httpx.Response(200, headers={"content-type": "image/png; x=1"},
                              stream=httpx.ByteStream(b"ok"))

    real = httpx.AsyncClient

    def client(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(mi.httpx, "AsyncClient", client)
    with pytest.raises(mi.NotFound):
        run(mi.http_fetch("https://images.example.com/big", None))
    with pytest.raises(mi.NotFound):
        run(mi.http_fetch("https://images.example.com/gzip", None))
    got = run(mi.http_fetch("https://images.example.com/move", None))
    assert got.status == 302 and got.location == "http://127.0.0.1/"
    got = run(mi.http_fetch("https://images.example.com/ok", PUBLIC_IP))
    assert (got.status, got.content_type, got.body) == (200, "image/png", b"ok")


# ------------------------------------------------------------------------------ the cache
def test_the_cache_serves_seven_days_then_refetches(data_dir):
    net = FakeNet({PROXY_ID: mi.Fetched(200, "image/jpeg", picture(400, 400))})
    first = load(net, PROXY_ID, now=1_000_000.0)
    files = list((data_dir / mi.CACHE_DIR).iterdir())
    assert [f.name for f in files] == [mi.cache_key(PROXY_ID, 256) + ".jpg"]
    assert load(net, PROXY_ID, now=1_000_000.0 + mi.CACHE_SECONDS - 1) == first
    assert len(net.calls) == 1
    load(net, PROXY_ID, 128, now=1_000_000.0)  # another size is another entry
    assert len(net.calls) == 2
    load(net, PROXY_ID, now=1_000_000.0 + mi.CACHE_SECONDS)
    assert len(net.calls) == 3


def test_the_cache_drops_the_oldest_past_the_cap(data_dir, monkeypatch):
    monkeypatch.setattr(mi, "CACHE_BYTES", 2500)
    folder = data_dir / mi.CACHE_DIR
    folder.mkdir()
    for i, name in enumerate(["a", "b", "c"]):
        path = folder / f"{name}.jpg"
        path.write_bytes(b"x" * 1000)
        os.utime(path, (1000 + i, 1000 + i))
    (folder / "old.png").write_bytes(b"x")
    os.utime(folder / "old.png", (0, 0))
    mi.evict(now=2000.0)
    assert sorted(p.name for p in folder.iterdir()) == ["b.jpg", "c.jpg"]


def test_eviction_is_throttled():
    assert mi.evict_due(1000.0, None, 1)
    assert not mi.evict_due(1000.0, 999.0, 1)
    assert mi.evict_due(1000.0, 999.0, mi.EVICT_EVERY)
    assert mi.evict_due(999.0 + mi.EVICT_SECONDS, 999.0, 1)
    assert mi.evict_due(500.0, 999.0, 1)  # the clock went back


def test_eviction_drops_old_tmp_leftovers(data_dir):
    folder = data_dir / mi.CACHE_DIR
    folder.mkdir()
    for name, age in (("old.1.tmp", mi.TMP_SECONDS + 1), ("new.2.tmp", 10)):
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (10_000 - age, 10_000 - age))
    mi.evict(now=10_000.0)
    assert [p.name for p in folder.iterdir()] == ["new.2.tmp"]


# ------------------------------------------------------------------------------ the route
@pytest.fixture
def api(data_dir, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        scopes = request.headers.get("x-test-scopes")
        request.state.api_token = scopes is not None
        request.state.api_token_owner = "alice" if scopes is not None else None
        request.state.api_token_scopes = scopes.split(",") if scopes else []
        return await call_next(request)

    net = FakeNet({PROXY_ID: mi.Fetched(200, "image/jpeg", picture(640, 640)),
                   CDN: mi.Fetched(200, "image/jpeg", picture(640, 640))},
                  dns={"images.example.com": [PUBLIC_IP]})
    monkeypatch.setattr(mi, "http_fetch", net.fetch)
    monkeypatch.setattr(mi, "resolve", net.resolve)
    monkeypatch.setenv("FORK_MA_URL", MA_URL)
    monkeypatch.delenv("FORK_MUSIC_IMAGES_INTERNET", raising=False)
    app.include_router(mi.setup_music_image_routes())
    with TestClient(app) as client:
        client.net = net
        yield client


def get(api, url, size=None, headers=VOICE):
    params = {"u": url} if url is not None else {}
    if size is not None:
        params["size"] = size
    return api.get(PREFIX + "/music/image", params=params, headers=headers)


def test_the_route_answers_the_image(api, caplog):
    caplog.set_level(logging.INFO, logger=mi.__name__)
    res = get(api, PROXY_ID, 128)
    assert res.status_code == 200 and res.headers["content-type"] == "image/jpeg"
    assert res.headers["cache-control"] == "max-age=604800"
    with Image.open(io.BytesIO(res.content)) as img:
        assert img.size == (128, 128)
    assert get(api, CDN).status_code == 200
    assert "secret-in-query" not in caplog.text and "imageproxy" not in caplog.text
    assert "example.com" not in caplog.text


def test_the_route_needs_the_voice_scope(api):
    assert get(api, PROXY_ID, headers={}).status_code == 403
    assert get(api, PROXY_ID, headers={"x-test-scopes": "chat"}).status_code == 403
    assert api.net.calls == []


@pytest.mark.parametrize(("url", "status", "error"), [
    ("http://127.0.0.1/", 403, "not allowed"),
    ("http://192.168.1.20:8123/imageproxy/1", 403, "not allowed"),
    ("https://10.0.0.1/a.jpg", 403, "not allowed"),
    ("ftp://example.com/a.jpg", 400, "bad url"),
    (None, 400, "bad url"),
    (MA_URL + "/imageproxy/missing", 404, "not found"),
])
def test_the_route_errors(api, url, status, error):
    res = get(api, url)
    assert (res.status_code, res.json()) == (status, {"error": error})


def test_the_internet_setting(api, monkeypatch):
    monkeypatch.setenv("FORK_MUSIC_IMAGES_INTERNET", "off")
    assert get(api, CDN).json() == {"error": "not allowed"}
    assert get(api, PROXY_ID).status_code == 200


def test_without_ma_url_only_the_internet_works(api, monkeypatch):
    monkeypatch.delenv("FORK_MA_URL")
    assert get(api, PROXY_ID).status_code == 403
    assert get(api, CDN).status_code == 200


def test_the_router_is_mounted():
    from fork.routes import ROUTERS

    assert ("fork.voice.music_images", "setup_music_image_routes") in ROUTERS
