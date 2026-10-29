# Local models

Thinking switches and vision-model detection for models on the owner's network.

Each section below was a row of the "Changes from upstream" table in
[FORK.md](../../../FORK.md) until 2026-10-29, moved here word for word; FORK.md now lists
the features and the hooks into upstream files.

## "Thinking: Off" on a local server marked api/proxy

**"Thinking: Off" on a local server marked api/proxy.** The switch (`chat_template_kwargs.enable_thinking`) was sent only to endpoints `is_local_endpoint` calls local, and an endpoint with an API key and a `/v1` URL on any port but 11434 is guessed to be a proxy. A local llama-server run with a key got no switch, so a Qwen3 model spent its reply thinking and returned no answer. A private host (loopback, LAN, Tailscale) now gets the switch whatever its kind; hosted providers still do not.

Files: `src/private_endpoint_thinking.py` (new), hook in `src/llm_core.py` (`_apply_local_qwen_thinking_mode`); test `tests/test_private_endpoint_thinking.py` (new)

Upstreamable: yes

## "Thinking: Off" works in Agent mode

**"Thinking: Off" works in Agent mode.** The agent loop builds the first model's request settings without the turn's `thinking_mode`, and that empty value replaced the caller's `off` (and the route's own Qwen choice). So in Agent mode llama-server never got `enable_thinking: false`: Qwen3.6 thought on every turn, the hub hid the thinking, and when the model stopped after thinking the chat showed an empty bubble. The primary model's route state now carries `thinking_mode`. Chat mode was not affected. General bug, not fork-specific.

Files: one line in `src/agent_loop.py` (`_active_route_state`); test `tests/test_agent_thinking_switch.py` (new)

Upstreamable: yes

## Vision auto-detect finds the owner's models

**Vision auto-detect finds the owner's models.** With Settings → Vision on Auto-detect, upstream tried only a fixed list of cloud and old model names (gpt-4o, claude-*, gemini-*, llava, pixtral, qwen2-vl), so an install with only local models found no vision model, though choosing the same model by hand worked. Auto-detect now takes the Utility model, then the Default chat model, when they read images, then any other enabled model that does (endpoints on the owner's network first), by the same check chat uses (`model_supports_vision`: what LM Studio reports, else the name); the old list stays as the last try. Handwriting (H2) uses the same search, local endpoints only.

Files: `src/vision_auto_detect.py` (new), hook in `src/document_processor.py` (`_resolve_vl_model`); fork file `src/ink_transcribe.py`; tests `tests/test_vision_auto_detect.py` (new)

Upstreamable: yes
