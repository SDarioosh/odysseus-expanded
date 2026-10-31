# Tool routing and grants

Which tools a turn is offered and allowed to run: slash skills, module questions, personal-store reads, chat or agent mode.

The chat route reaches all of these through `fork/routing/turn_policy.py`: one function per
hook in `routes/chat_routes.py`, in the route's order (`scope_tool_intent`,
`escalate_chat_mode`, `extend_authority`, `extend_contract`, `admit_offered_reads`; the
warm-tools and Auto/Ask form hooks call their own files). Its docstring says why the hooks
sit where they do; `tests/fork/test_turn_policy_characterization.py` pins the decisions
for a table of turns through the real route.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## Web-intent routing

**Web-intent routing.** Questions with words like "latest" or "recommend" no longer get an instant "Web access is disabled" reply when trusted read-only MCP tools are available.

Files: hook in `src/agent_loop.py` (`_web_search_unavailable_for_turn`)

Upstreamable: yes

## MCP tools in skill `requires_toolsets`

**MCP tools in skill `requires_toolsets`.** A skill can require `mcp:<server name or id>` (every enabled tool of a connected server) or a qualified `mcp__<server>__<tool>`. They expand to the real tool names wherever `requires_toolsets` is checked or applied: skill matching, the skill index's visibility gate, active skills, and skills the model loads with `manage_skills view`. Only connected servers and enabled tools count; write tools keep their approval.

Files: `src/skill_mcp_toolsets.py` (new), 4 hooks in `src/agent_loop.py`

Upstreamable: yes (issue draft in SDarioosh/odysseus-expanded#9)

## Slash-invoked skills bring their tools

**Slash-invoked skills bring their tools.** In "Auto", the route's turn contract chose tools from the message words and replaced what the agent loop added for a skill, so `/<skill>` got its `requires_toolsets` only when the SKILL.md text named the tool, and the SKILL.md text in the composed message could itself be misread (a "search skills" request, refused). The invoke route's message is now recognized server-side; the skill is looked up in the sender's own invokable skills and the turn is offered exactly its declared tools plus `ask_user`/`update_plan`. Current message only, no state. Browsers submit textarea line breaks as CRLF, so the message is matched after normalizing `\r\n`.

Files: `src/skill_invocation.py` (new), called from `extend_contract` in `fork/routing/turn_policy.py` (hook in `routes/chat_routes.py` after `resolve_turn_contract`)

Upstreamable: yes

## Slash-invoked skills run in agent mode

**Slash-invoked skills run in agent mode.** The web UI sends `mode=chat` unless the agent toggle is on, and chat mode never builds a turn contract, so a slash-invoked skill's pinned tools never applied: `/sound` reached the model with no tools at all (the lexical tool-intent classifier doesn't see a tool request in the composed skill message). A slash-invoked skill that declares `requires_toolsets` now switches that turn to agent mode, like the other chat→agent escalations.

Files: `slash_skill_needs_tools` in `src/skill_invocation.py`, called from `escalate_chat_mode` in `fork/routing/turn_policy.py` (hook in `routes/chat_routes.py` after the chat→agent escalation)

Upstreamable: yes

## Follow-ups keep a used module's tools

**Follow-ups keep a used module's tools.** On a message without a slash command, upstream offers back only the exact tools that already ran in the chat. After `/sound` searched a conversation, "Speaker 2 was Kath" got the search tools but not `name_speaker`, so the model said it had no such tool. Now a tool from a server in `ODYSSEUS_TRUSTED_READONLY_MCP` that ran earlier in the chat brings back that server's other enabled tools. Offered only: write tools still ask for approval; policy and per-tool disables still apply.

Files: `with_trusted_server_tools` in `src/mcp_trust.py`, hook in `routes/chat_routes.py` (after `_successful_session_tool_names`)

Upstreamable: yes

## An approved module tool covers the rest of the task

**An approved module tool covers the rest of the task.** The chat approval card lifts the approval gate for the rest of the resumed task, but request authority admitted only the one exact approved MCP call, so after approving one `name_speaker` the next ones ("Speaker 3", "Speaker 2") were blocked as "outside server request authority". Now, when the approved tool is on a server in `ODYSSEUS_TRUSTED_READONLY_MCP`, the resumed task gets a grant for that same tool only, plus its MCP backend in the authority's sealed backend list (without it the calls failed with "External backend is outside sealed request scope"). Policy and per-tool disables still apply; other tools get nothing.

Files: `with_approved_tool_grant` in `src/mcp_trust.py`, called from `extend_authority` in `fork/routing/turn_policy.py` (hook in `routes/chat_routes.py` after the exact-approval authority)

Upstreamable: yes, with discussion

## Several tools in a row

**Several tools in a row.** Upstream grants a turn only the tools its message words name ("what are my tasks for today?" → `manage_tasks`), but the model is often shown more: tools used earlier in the chat, and every enabled tool on the run that resumes after the approval card or with a "Regular tools" model. So after the tasks lookup, the model's calendar call was refused ("disabled by user or server request authority policy" / "outside server request authority"). Now every personal store the turn offers (calendar, tasks, notes, memory, documents, skills) gets its read-only actions (`list_events`, `list`, `search`, `view` …, from upstream's `_PRIVATE_ACTION_READS`). Writes still need the words or an approval; user, owner, global, incognito and plan-mode disables still win; admin and session tools are left out.

Files: `src/offered_reads.py` (new), called from `admit_offered_reads` in `fork/routing/turn_policy.py` (hook in `routes/chat_routes.py` after the final turn policy)

Upstreamable: yes, with discussion

## Store lookups leave chat mode

**Store lookups leave chat mode.** The web UI starts in chat mode, and upstream promotes a chat-mode turn to agent mode only for the actions `src/action_intents.py` lists (add to calendar, send an email, set a reminder, run a command ...): it has no contacts pattern and none for a plain lookup, so "list the contact for Alex Example" or "what are my tasks for today?" stayed in chat mode with no tools, and the model answered from pinned memory or invented the record, with no tool card. A chat-mode message that the agent-mode classifier (`requested_capabilities`) puts in a personal-store family (contacts, calendar, tasks, notes, memory) now runs in agent mode, where the turn contract offers that store's tool. "no tools" in the message and slash commands are left alone. Off with `ODYSSEUS_PERSONAL_STORE_ESCALATION=0`. **No new hook:** it runs from the slash-skill hook, which hands a message without a slash command to the new file.

Files: `src/personal_store_escalation.py` (new), called from `needs_agent_mode` in `fork/routing/turn_policy.py`; test `tests/fork/test_personal_store_escalation.py` (new)

Upstreamable: yes, with discussion

## Reads of your own stores without exact wording

**Reads of your own stores without exact wording.** Upstream grants a tool only when the message words name it, so "notate that Katherine is my fiance" or a pronoun follow-up got no grant and the model's `manage_contact` call was blocked as "outside server request authority"; a small local model could not recover. Every turn now also grants the read actions (list, search, find, view) of `manage_contact`, `manage_notes`, `manage_calendar`, `manage_tasks`, `manage_memory`, `manage_documents`, `manage_research` and `manage_skills`, taken from `_PRIVATE_ACTION_READS`. Writes and deletes keep their gate. Tool policy and per-tool disables still win. Off with `ODYSSEUS_PERSONAL_READ_GRANTS=0`.

Files: `src/personal_read_grants.py` (new), called from `extend_authority` in `fork/routing/turn_policy.py`, after the web-search grant; test `tests/fork/test_personal_read_grants.py` (new)

Upstreamable: yes, with discussion

## Plain module questions reach the module's tools

**Plain module questions reach the module's tools.** Without `/sound`, a question like "What did Alice say about the budget?" got no module tools: chat mode (the web UI default) stayed in chat mode, and the agent-mode turn contract has no family for MCP tools. A plain message with a module cue (conversation words such as "said", "told me", "meeting", "promised"; "my next/my rent…" questions; or a person the module's `list_people` knows, cached for 10 minutes and refreshed in the background) now runs in agent mode, and turns with no family, only personal-store families, or a cue are offered the trusted modules' **read-only** tools; on a cue with no family, bash/python/read_file are dropped. "Conversation" without a chat word ("chat", "session", "thread", "our/this conversation", "with you") means a recording: "what is my last conversation?" swaps the chat-history (`sessions`) tools for the module's reads instead of answering with `list_sessions`. Offers only: writes, policy and disables are unchanged. **No new hook:** it runs from the two slash-skill hooks, which hand a message without a slash command to the new file.

Files: `src/module_turn_scope.py` (new), called from `needs_agent_mode` and `scoped_contract` in `fork/routing/turn_policy.py`; test `tests/fork/test_module_turn_scope.py` (new)

Upstreamable: with discussion

## Managed skills

**Managed skills.** Skills with `source: managed` (created by `modules_setup.py`) are slash-invokable without the LLM skill audit that "Auto-approve skills" requires, and an audit verdict doesn't hide them. Only the owner's own skills are affected.

Files: hook in `services/memory/skills.py` (`index_for`), check in `src/skill_invocation.py`

Upstreamable: with discussion

## Chat-mode questions aren't run

**Chat-mode questions aren't run.** Upstream moves a chat-mode turn to agent mode and turns bash on when `src/action_intents.py` matches a shell or workspace pattern, and some of those are broad: "my home assistant on port 8123" matched "named computer task request" (any "on <word>"), and a bare "docker" or "git" matches "terminal workspace request". So "why can I not access ... how can I check if my server has port 8123 active?" came back as an approval card for `ssh ... "ss -tlnp"` instead of an answer. In chat mode, a shell/workspace match on a how/why question that asks the assistant to do nothing (no "can you ...", "please", "for me", or a sentence starting with run/check/ssh ...) now stays in chat mode. Agent mode, the web toggle and the other categories are unchanged.

Files: `src/chat_mode_scope.py` (new), called from `scope_tool_intent` in `fork/routing/turn_policy.py` (hook in `routes/chat_routes.py` after `_classify_tool_intent`); test `tests/fork/test_chat_mode_scope.py` (new)

Upstreamable: yes
