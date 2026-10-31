"""Agent mode sends the turn's thinking switch to the primary model (src/agent_loop.py).

The owner had Thinking off in Agent mode, but qwen3.6-35b thought anyway and
some replies ended as an empty bubble: the primary model's request kwargs
carried thinking_mode=None, so llama-server never got enable_thinking=false.
"""
import json

import src.agent_loop as al
from tests.test_agent_rounds_exhausted import _collect, _patch_common

QUESTION = "How do I allow installs from unknown apps on my phone?"


def _sent_thinking_modes(monkeypatch, **loop_kwargs):
    """The thinking_mode each model request is sent with, after the route's per-model kwargs."""
    _patch_common(monkeypatch)
    sent = []

    async def _no_compaction(*args, **kwargs):
        return args[3], 0, False

    async def _fake_stream(candidates, messages, **kwargs):
        mode = kwargs.get("thinking_mode")
        factory = kwargs.get("candidate_request_factory")
        if factory is not None:
            url, model, headers = candidates[0]
            request = factory(0, url, model, headers)
            if hasattr(request, "__await__"):
                request = await request
            mode = {**kwargs, **((request or {}).get("kwargs") or {})}.get("thinking_mode")
        sent.append(mode)
        yield "data: " + json.dumps({"delta": "Open Settings."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "maybe_compact", _no_compaction, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        "http://x/v1", "qwen3.6-35b",
        [{"role": "user", "content": QUESTION}],
        max_rounds=5,
        owner="admin",
        **loop_kwargs,
    ))
    return sent


def test_thinking_off_reaches_the_primary_model(monkeypatch):
    assert _sent_thinking_modes(monkeypatch, thinking_mode="off") == ["off"]


def test_thinking_on_reaches_the_primary_model(monkeypatch):
    assert _sent_thinking_modes(monkeypatch, thinking_mode="on") == ["on"]
