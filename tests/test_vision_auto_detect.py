"""Fork: Settings → Vision "Auto-detect" finds the owner's own models (src/vision_auto_detect.py).

A temporary endpoints table with fictional local and cloud endpoints.
"""
import json

import pytest

import core.database as cdb
import src.endpoint_resolver as er
from core.database import ModelEndpoint
from src import vision_auto_detect as vad
from tests.helpers.sqlite_db import make_temp_sqlite

LOCAL = "http://127.0.0.1:8080/v1"
CLOUD = "https://api.example.com/v1"


@pytest.fixture
def db(monkeypatch):
    factory, engine, _tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(er, "SessionLocal", factory)
    # No Utility / Default model set unless a test says so.
    monkeypatch.setattr(er, "resolve_endpoint", lambda prefix, owner=None: (None, None, None))
    # Never probe LM Studio from tests: decide by name only.
    import src.chat_helpers as ch
    monkeypatch.setattr(ch, "lmstudio_supports_vision", lambda url, model: None)
    monkeypatch.setattr(vad, "_is_local", lambda url: "127.0.0.1" in (url or ""))
    yield factory
    engine.dispose()


def add(factory, ep_id, base, models, *, hidden=(), enabled=True, model_type="llm"):
    s = factory()
    s.add(ModelEndpoint(
        id=ep_id, name=ep_id, base_url=base, is_enabled=enabled, model_type=model_type,
        cached_models=json.dumps(list(models)), hidden_models=json.dumps(list(hidden)),
    ))
    s.commit()
    s.close()


def test_finds_a_local_model_the_old_name_list_missed(db):
    add(db, "llama", LOCAL, ["text-embedding-nomic", "qwen3.5-35b-a3b"])
    url, model, _headers = vad.auto_detect_vision_model()
    assert model == "qwen3.5-35b-a3b"
    assert url.startswith(LOCAL)


def test_prefers_local_endpoints_over_cloud(db):
    add(db, "cloud", CLOUD, ["gpt-4o"])
    add(db, "local", LOCAL, ["gemma3:12b"])
    assert vad.auto_detect_vision_model()[1] == "gemma3:12b"
    assert vad.auto_detect_vision_model(local_only=True)[1] == "gemma3:12b"


def test_local_only_leaves_cloud_out(db):
    add(db, "cloud", CLOUD, ["gpt-4o"])
    assert vad.auto_detect_vision_model()[1] == "gpt-4o"
    assert vad.auto_detect_vision_model(local_only=True) is None


def test_skips_hidden_disabled_text_only_and_non_chat_models(db):
    add(db, "a", LOCAL, ["qwen3-vl-8b", "tinyllama-1b"], hidden=["qwen3-vl-8b"])
    add(db, "b", LOCAL, ["gemma3:4b"], enabled=False)
    add(db, "c", LOCAL, ["qwen3-vl-embedding-2b"])
    add(db, "d", LOCAL, ["qwen-image-vl"], model_type="image")
    assert vad.auto_detect_vision_model() is None


def test_utility_model_comes_first_when_it_reads_images(db, monkeypatch):
    add(db, "local", LOCAL, ["gemma3:12b"])
    routes = {"utility": (LOCAL + "/chat/completions", "qwen2.5-vl-7b", {}),
              "default": (LOCAL + "/chat/completions", "llama-3.1-8b", {})}
    monkeypatch.setattr(er, "resolve_endpoint", lambda prefix, owner=None: routes[prefix])
    assert vad.auto_detect_vision_model()[1] == "qwen2.5-vl-7b"
    routes["utility"] = (LOCAL + "/chat/completions", "llama-3.1-8b", {})
    assert vad.auto_detect_vision_model()[1] == "gemma3:12b"


def test_resolve_vl_model_auto_uses_the_configured_models(db):
    from src.document_processor import _resolve_vl_model

    add(db, "local", LOCAL, ["qwen3.5-9b"])
    assert _resolve_vl_model("")[1] == "qwen3.5-9b"
