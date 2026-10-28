"""No MCP or module token in the hub's logs (src/log_redact.py, src/call_recorder.py,
src/record_share.py). Fictional tokens only."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import httpx
import pytest

from src import call_recorder as rec
from src import log_redact, record_share

TOKEN = "fictional-mcp-token-123"
LINK = rec.Link(base="http://module:8001", token=TOKEN)


def _logger_with_buffer() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    lg = logging.getLogger(f"test_log_redact.{id(buf)}")
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    log_redact.install(lg)
    return lg, buf


@pytest.mark.parametrize("text,want", [
    (f"POST http://m:8001/mcp?access_token={TOKEN}", "POST http://m:8001/mcp?access_token=***"),
    (f"GET http://m/x?since=5&access_token={TOKEN}&limit=2",
     "GET http://m/x?since=5&access_token=***&limit=2"),
    (f'"http://m/mcp?ACCESS_TOKEN={TOKEN}"', '"http://m/mcp?ACCESS_TOKEN=***"'),
    (f"http://m/cb?code=1&refresh_token={TOKEN}#frag", "http://m/cb?code=1&refresh_token=***#frag"),
    ("http://m/x?sort_key=a&monkey=b", "http://m/x?sort_key=a&monkey=b"),  # untouched
    ("no url here", "no url here"),
])
def test_redact(text, want):
    assert log_redact.redact(text) == want


def test_filter_masks_message_args():
    lg, buf = _logger_with_buffer()
    lg.info('HTTP Request: %s %s "%s"', "POST",
            httpx.URL(f"http://module:8001/mcp?access_token={TOKEN}"), "HTTP/1.1 200 OK")
    out = buf.getvalue()
    assert TOKEN not in out and "access_token=***" in out


def test_filter_masks_tracebacks():
    lg, buf = _logger_with_buffer()
    try:
        raise RuntimeError(f"failed at http://module:8001/mcp?access_token={TOKEN}")
    except RuntimeError:
        lg.exception("call failed")
    out = buf.getvalue()
    assert TOKEN not in out and "access_token=***" in out and "call failed" in out


def test_install_is_idempotent():
    lg, _ = _logger_with_buffer()
    log_redact.install(lg)
    assert sum(isinstance(f, log_redact.RedactFilter) for f in lg.handlers[0].filters) == 1


def test_httpx_request_log_has_no_token(caplog):
    """The real leak: httpx's own INFO line for a URL that carries the token (the MCP client)."""
    root = logging.getLogger()
    handler = logging.StreamHandler(buf := io.StringIO())
    root.addHandler(handler)
    try:
        log_redact.install(root)
        caplog.set_level(logging.INFO, logger="httpx")
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        with httpx.Client(transport=transport) as client:
            client.post(f"http://module:8001/mcp?access_token={TOKEN}")
    finally:
        root.removeHandler(handler)
    out = buf.getvalue()
    assert "HTTP Request" in out and TOKEN not in out


def test_app_installs_the_filter():
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text()
    assert "src.log_redact import install" in src


# ---- the fork's own module calls send the token as a header, never in the URL --------

class _Module:
    def __init__(self, body=None):
        self.requests: list[httpx.Request] = []
        self.body = body if body is not None else {"ok": True}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return httpx.Response(200, json=self.body)


def _assert_header_only(req: httpx.Request) -> None:
    assert TOKEN not in str(req.url)
    assert "access_token" not in req.url.params
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


def test_forward_sends_token_as_header():
    module = _Module()
    status, _ = asyncio.run(rec.forward(LINK, "GET", "/action-items", params={"since": 5},
                                        transport=httpx.MockTransport(module)))
    assert status == 200
    _assert_header_only(module.requests[0])
    assert dict(module.requests[0].url.params) == {"since": "5"}


def test_upload_sends_token_as_header():
    module = _Module()
    asyncio.run(rec._post(LINK, "/ingest/chunk", httpx.MockTransport(module),
                          files={"audio": ("c.ogg", b"OggS", "audio/ogg")}))
    _assert_header_only(module.requests[0])


def test_share_export_sends_token_as_header():
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"# Title\n", headers={
            "content-type": "text/markdown", "content-disposition": 'attachment; filename="a.md"'})
    seen: list[httpx.Request] = []
    try:
        asyncio.run(record_share.fetch(LINK, "c1", "md", ("summary",),
                                       transport=httpx.MockTransport(handler)))
    except Exception:
        pass  # only the request matters here
    _assert_header_only(seen[0])
    assert seen[0].url.params["format"] == "md"
