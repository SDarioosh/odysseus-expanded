"""Shared fixtures for the fork's own tests (``tests/fork/``).

Only setup that several fork test files repeated the same way lives here; a file whose
setup differs keeps its own. The fake MCP manager is a plain class in
``tests/fork/fakes.py`` (test modules subclass it at import time, which a fixture can't
serve). Fictional data only.

There is deliberately no ``tests/fork/__init__.py``: as a package, ``tests/fork`` could
be found as a top-level ``fork`` and shadow the fork's code package at the repo root
(``test_fork_tests_layout.py`` checks that ``import fork`` still gets the real one).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """``src.constants.DATA_DIR`` pointed at this test's ``tmp_path``, which it returns.

    The fork's stores read ``DATA_DIR`` at call time, so this keeps every file a test
    writes (ledgers, insight files, ink pages) out of the real ``data/``.
    """
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_local_model(monkeypatch):
    """The background local model, faked at its three edges.

    ``interactive_gate.wait_for_interactive_quiet`` records the label it waited for in
    ``.waited``; ``llm_core.llm_call_async_with_fallback`` records each call's keyword
    arguments in ``.sent`` and answers ``.answer``; ``task_endpoint`` resolves one
    candidate. Use it to check how a fork task calls the model (workload, thinking,
    token budget), not what the model says: tests of the answers pass their own fake.
    """
    import src.interactive_gate as gate
    import src.llm_core as llm_core  # noqa: PLR0402
    import src.task_endpoint as te

    model = SimpleNamespace(waited=[], sent=[], answer="{}")

    async def fake_wait(label):
        model.waited.append(label)

    async def fake_call(cands, messages, **kw):
        model.sent.append(kw)
        return model.answer

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", fake_wait)
    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_call)
    monkeypatch.setattr(te, "resolve_task_candidates", lambda **kw: [("http://llm", "m", {})])
    return model
