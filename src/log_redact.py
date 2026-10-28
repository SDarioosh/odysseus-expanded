"""Keep secrets in URLs out of the logs.

httpx logs every request URL at INFO ("HTTP Request: POST http://h/mcp?access_token=…"),
and app.py sends INFO to the console and ``data/logs/app.log``. An MCP server whose URL
carries its token (Athena's ``?access_token=``, the only form Odysseus's MCP client can
send) would put that token in the log on every tool call. ``install`` adds a filter to the
root handlers that replaces such query values with ``***`` in every record, messages and
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


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # a bad format string: leave it to the handler's own error path
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            # Formatter.format uses exc_text when set; an httpx error's text holds its URL
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Add the filter to every handler of ``logger`` (default: root). Idempotent."""
    logger = logger or logging.getLogger()
    for handler in logger.handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())
