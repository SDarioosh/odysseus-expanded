"""Fork: fork/common/llm.py, the background local-model JSON calls."""
from __future__ import annotations

import asyncio
import logging

import pytest

from fork.common import llm


@pytest.mark.parametrize(("raw", "want"), [
    (None, None), ("", None), ("no braces", None), ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}), ('<think>{"x": 0}</think>{"a": 1}', {"a": 1}),
    ('Here: {"a": {"b": 2}} done', {"a": {"b": 2}}), ('{"a": 1} {"b": 2}', None),
    ("} {", None), ("[1, 2]", None),
])
def test_json_object(raw, want):
    assert llm.json_object(raw) == want


@pytest.mark.parametrize("wait_quiet", [True, False])
def test_background_model_call(fake_local_model, wait_quiet):
    async def go():
        call = await llm.background_model("alice", "job", temperature=0.3, max_tokens=50,
                                          wait_quiet=wait_quiet)
        return await call([{"role": "user", "content": "q"}])

    assert asyncio.run(go()) == "{}"
    assert fake_local_model.waited == (["job"] if wait_quiet else [])
    assert fake_local_model.sent == [{
        "temperature": 0.3,
        "max_tokens": 50, "timeout": 180, "thinking_mode": "off",
        "workload": "background" if wait_quiet else "foreground"}]


def test_ask_with_retry_sends_the_correction_once(caplog):
    answers = iter(["bad", "worse"])
    sent = []

    async def model(messages):
        sent.append(messages)
        return next(answers)

    def check(raw):
        return None, ("was refused", "Fix it.")

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(llm.ask_with_retry(model, [{"role": "user", "content": "q"}], check,
                                             label="job", log=logging.getLogger("t")))
    assert out is None
    assert sent[1][1:] == [{"role": "assistant", "content": "bad"},
                           {"role": "user", "content": "Fix it."}]
    assert caplog.messages == ["job: model answer 1 was refused",
                               "job: model answer 2 was refused"]
