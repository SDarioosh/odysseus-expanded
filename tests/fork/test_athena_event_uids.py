"""Fork: the uids of calendar events Athena's action items filed, pinned.

A filed event's uid (``_event_uid``, ``fork/athena/action_items_state.py``) is how the hub
recognises it on the next run; a changed uid files every event again. These are the uids
of today's events (server ``convo``, the default) for a few fictional items, written out.
"""
from __future__ import annotations

import pytest

from fork.athena import action_items as mai

TODAY = {  # (item id, revision) -> uid
    (1, 0): "7e31c3fd-ecba-595d-ba38-86ed451b113e",
    (12, 0): "d8a15a0a-d2b1-5246-a8b9-8d844527d6b7",
    (12, 1): "b3204101-739b-5275-8744-a71f1c52ccc7",
    (9999, 2): "ea91d2fe-fad7-56e5-aec8-b07e98da74b9",
}
ENV = ("MODULE_ACTION_ITEMS_SERVER", "BARVIS_ACTION_ITEMS_SERVER")


@pytest.mark.parametrize("server", [None, "convo", "Convo", " convo "])
def test_filed_event_uids_never_change(monkeypatch, server):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    if server is not None:
        monkeypatch.setenv("MODULE_ACTION_ITEMS_SERVER", server)
    assert {key: mai._event_uid(*key) for key in TODAY} == TODAY
