"""Keep secrets in URLs out of the logs.

httpx logs every request URL at INFO ("HTTP Request: POST http://h/mcp?access_token=…"),
and app.py sends INFO to the console and ``data/logs/app.log``. An MCP server whose URL
carries its token (Athena's ``?access_token=``, the only form Odysseus's MCP client can
send) would put that token in the log on every tool call. ``install`` wraps the formatter
of the root handlers so such query values are written as ``***``, in messages and
tracebacks alike.
"""

from __future__ import annotations

import logging
import re

# Query parameters whose value is a secret. "key" alone is left out: too many harmless
# uses (cache keys, sort keys).
_SECRET_PARAMS = ("access_token", "refresh_token", "id_token", "token", "api_key", "apikey",
                  "client_secret", "password", "secret")
_SECRET_QUERY = re.compile(
    r"([?&](?:%s)=)[^&\s\"'<>#]+" % "|".join(_SECRET_PARAMS), re.IGNORECASE)
MASK = "***"


def redact(text: str) -> str:
    """``…?access_token=abc&x=1`` -> ``…?access_token=***&x=1``."""
    return _SECRET_QUERY.sub(lambda m: m.group(1) + MASK, text)


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's formatter and masks secrets in the text it writes. The record
    itself is left alone, so other handlers (and their tests) see it unchanged."""

    def __init__(self, inner: logging.Formatter | None) -> None:
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        # the message, and the traceback, whose httpx error text holds the request URL
        return redact(self.inner.format(record))


def install(logger: logging.Logger | None = None) -> None:
    """Mask secrets in what every handler of ``logger`` (default: root) writes.
    Idempotent."""
    logger = logger or logging.getLogger()
    for handler in logger.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))
