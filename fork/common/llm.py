"""Fork: background calls to the local model that answer with one JSON object.

* :func:`background_model`: the background-task model chain for an owner
  (``src.task_endpoint.resolve_task_candidates``), thinking off; before each call it
  waits for chat to go quiet (``workload="background"``), or, with ``wait_quiet=False``
  (the owner is waiting for the answer), it doesn't (``workload="foreground"``).
* :func:`json_object`: the JSON object in a model answer: ``<think>`` blocks and code
  fences removed, the text from the first ``{`` to the last ``}`` parsed; None when
  there is none.
* :func:`ask_with_retry`: one retry: an answer the check refuses is sent back with the
  check's correction, and the second answer is the last.

Used by belief structure, notes evidence and today insight. The ink reader
(``fork/ink/transcribe.py``) calls the model its own way: it doesn't wait for quiet,
tries each candidate itself and records which one answered.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

Llm = Callable[[list[dict]], Awaitable[str]]
# (result, None) when the answer is usable, else (None, (problem, correction)).
Check = Callable[[Any], tuple[Any, tuple[str, str] | None]]

FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


async def background_model(owner: str | None, label: str, *, temperature: float,
                           max_tokens: int, timeout: float = 180,
                           wait_quiet: bool = True) -> Llm:
    """A model caller for ``owner``; raises ``RuntimeError`` when no model endpoint is
    configured. ``label`` names the job while it waits for chat to go quiet."""
    from src.interactive_gate import wait_for_interactive_quiet
    from src.llm_core import llm_call_async_with_fallback
    from src.task_endpoint import resolve_task_candidates

    candidates = resolve_task_candidates(owner=owner or None)
    if not candidates:
        raise RuntimeError("no model endpoint configured")

    async def call(messages: list[dict]) -> str:
        if wait_quiet:
            await wait_for_interactive_quiet(label)
        return await llm_call_async_with_fallback(
            candidates, messages=messages, temperature=temperature, max_tokens=max_tokens,
            timeout=timeout, thinking_mode="off",
            workload="background" if wait_quiet else "foreground")

    return call


def json_object(raw: str | None) -> Any:
    """The parsed ``{...}`` in a model answer, or None."""
    text = raw or ""
    try:
        from src.text_helpers import strip_think

        text = strip_think(text, prose=False, prompt_echo=False)
    except Exception:  # noqa: BLE001
        text = THINK.sub("", text)
    text = FENCE.sub("", text.strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


async def ask_with_retry(llm: Llm, messages: list[dict], check: Check, *, label: str,
                         log: logging.Logger) -> Any:
    """The checked result of the first usable answer of two; None when both fail. Each
    refused answer is logged on ``log`` as ``<label>: model answer <n> <problem>``."""
    for attempt in range(2):
        raw = await llm(messages)
        result, retry = check(raw)
        if retry is None:
            return result
        problem, fix = retry
        log.warning("%s: model answer %d %s", label, attempt + 1, problem)
        messages = [*messages, {"role": "assistant", "content": str(raw or "")[:2000]},
                    {"role": "user", "content": fix}]
    return None
