# MCP servers

How the fork connects to MCP servers, trusts the read-only ones and recovers lost connections.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Trusted read-only MCP servers

**Trusted read-only MCP servers.** Servers named in `ODYSSEUS_TRUSTED_READONLY_MCP` get their `readOnlyHint: true` tools granted on every turn, without per-call approval and without the "outside server request authority" block on follow-up calls. Write tools and every other server keep the normal approval flow.

Files: `src/mcp_trust.py` (new), hooks in `src/agent_runtime/authority.py` and `src/tool_capabilities.py`

Upstreamable: yes

## Trusted reads aren't external effects

**Trusted reads aren't external effects.** The evidence ledger counted every MCP call as an unverified external change, which appended "External operation … not independently verified" to answers and sometimes replaced the answer with it. Trusted read-only calls are now admitted reads.

Files: hook in `src/agent_runtime/effect_adapters.py` (`classify`)

Upstreamable: yes

## Env reference entry

**Env reference entry.** Upstream's `tests/test_env_reference.py` requires every `ODYSSEUS_*` variable the code reads to have a note, so the fork's variables have one (`ODYSSEUS_TRUSTED_READONLY_MCP`, `ODYSSEUS_PERSONAL_READ_GRANTS`, `ODYSSEUS_PERSONAL_STORE_ESCALATION`, `ODYSSEUS_USER`) and `website/configuration-reference.md` is regenerated with them.

Files: hook in `scripts/generate_env_reference.py` (`VARIABLE_NOTES`), generated `website/configuration-reference.md`

Upstreamable: with the trusted read-only change

## HTTP MCP servers keep tool annotations

**HTTP MCP servers keep tool annotations.** `_connect_http` dropped each tool's `annotations` (stdio and SSE kept them), so `readOnlyHint` was lost for Streamable HTTP servers: trusted read-only tools still needed approval, and plan mode fell back to the name heuristic. Now stored the same way as stdio/SSE. General bug, not fork-specific.

Files: one line in `src/mcp_manager.py` (`_connect_http`); test `tests/fork/test_mcp_http_annotations.py` (new)

Upstreamable: yes (issue to file)

## Stdio MCP servers keep working after the request that added them

**Stdio MCP servers keep working after the request that added them.** A stdio server added, enabled or reconnected over the API was connected inside the HTTP request; `BaseHTTPMiddleware` cancels its task group once the response is sent, which also cancelled the stdio transport, so every later call failed with an empty error (`MCP tool call failed: <name>:`, sandbox finding 2). Every stdio connection now opens in its own long-lived owner task (fresh context) and is held there until `disconnect_server`, the way built-in servers already were; disconnects close it from that task. General bug, not fork-specific.

Files: `src/mcp_stdio_owner.py` (new), hooks in `src/mcp_manager.py` (`connect_server`, `hold_owned_connection`); test `tests/fork/test_mcp_stdio_owner.py` (new)

Upstreamable: yes (issue to file)

## Lost MCP sessions reconnect on their own

**Lost MCP sessions reconnect on their own.** When an HTTP server restarted (an outage, a module redeploy), it no longer knew Odysseus' session, so every call failed with "Session terminated" while Settings still showed it connected, until Reconnect was pressed; upstream only reconnects built-in stdio servers. A non-built-in HTTP/SSE server whose call fails that way (or whose transport is closed) is now reconnected from its stored config and the call is retried once: the server refused it, so the tool didn't run. Module servers (`ODYSSEUS_TRUSTED_READONLY_MCP` and the action-items server) also get a 60 s deadline per call: while one was down the MCP client never answered, even after Reconnect, so a call hung for good, and the "Module Action Items" task, stuck in flight, never ran again (voice-note to-dos stopped being filed). A call past the deadline returns an error and reconnects without a retry. A server that is still down is retried in the background every minute for a day, and connected once it answers. Not inside a turn's sealed backend call (new connection = new identity). General bug, not fork-specific.

Files: `src/mcp_session_recovery.py` (new), 2 hooks in `src/mcp_manager.py` (`call_tool`); test `tests/fork/test_mcp_session_recovery.py` (new)

Upstreamable: yes (issue to file)

## Empty replies after module tools get an answer

**Empty replies after module tools get an answer.** After `/sound` searched and opened a conversation, the model sometimes returned no text, and the chat showed only the placeholder "Done.". Upstream already runs one tool-free "your last response was empty, answer now from the evidence above" round, but only for unattended runs. A web-chat turn whose reply is empty after a successful tool on a server in `ODYSSEUS_TRUSTED_READONLY_MCP` now gets that same round. Not when the turn stopped on an approval or question card.

Files: `ran_trusted_server_tool` in `src/mcp_trust.py`, hook in `src/agent_loop.py` (end-of-loop empty-final recovery); test `tests/fork/test_mcp_empty_answer.py` (new)

Upstreamable: yes
