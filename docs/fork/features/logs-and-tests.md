# Logs and tests

Tokens kept out of the logs, and an upstream test fixed so it doesn't leak into others.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Test isolation: a fresh `core.middleware` no longer leaks

**Test isolation: a fresh `core.middleware` no longer leaks.** `test_legacy_reserved_username_session_cannot_pass_admin_gate` re-imported `core.middleware` with `clear_module` and left the fresh copy in `sys.modules`, so a later test in the same run that overrides `require_admin` on routes imported earlier got 403 (`tests/test_today_insight.py::test_refresh_route_returns_202_and_runs_in_the_background`, whenever the shard plan puts both files in one shard, as adding `tests/test_meeting_bot.py` did). It now evicts the module with `monkeypatch`, which restores the original afterwards.

Files: `tests/test_reserved_username_admin_escalation.py` (upstream test, a few lines marked `# Fork:`)

Upstreamable: yes

## No tokens in the logs

**No tokens in the logs.** httpx logs every request URL at INFO and `app.py` sends INFO to the console and `data/logs/app.log`, so an MCP server URL with `?access_token=` (Athena's, the only form the MCP client can send) put the token in the log on every tool call. The root handlers' formatters write the values of `access_token`, `token`, `api_key` and similar query parameters as `***` in messages and tracebacks; the records stay unchanged for other handlers. The fork's own module calls (call recorder, share export, ink, settings pages, meeting bot) send the token as `Authorization: Bearer` instead.

Files: `src/log_redact.py`; hook in `app.py` (after the logging handlers); `src/call_recorder.py`, `src/record_share.py`; `tests/test_log_redact.py`

Upstreamable: yes
