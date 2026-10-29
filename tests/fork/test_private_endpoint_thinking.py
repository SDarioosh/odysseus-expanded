"""Thinking switch on private endpoints (src/private_endpoint_thinking.py)."""
import pytest

from src import llm_core
from src.private_endpoint_thinking import is_private_host


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:8082/v1", True),
    ("http://127.0.0.1:8082/v1", True),
    ("http://192.168.1.20:8082/v1", True),
    ("http://100.64.3.4:8082/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://openrouter.ai/api/v1", False),
    ("", False),
])
def test_is_private_host(url, expected):
    assert is_private_host(url) is expected


@pytest.mark.parametrize("kind", ["proxy", "api", "auto"])
def test_off_reaches_a_private_endpoint_of_any_kind(monkeypatch, kind):
    monkeypatch.setattr("src.model_context._configured_endpoint_kind", lambda url: kind)
    payload = {}
    llm_core._apply_local_qwen_thinking_mode(
        payload, "http://localhost:8082/v1", "qwen3.5-9b-aggressive", "off")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_hosted_endpoint_still_gets_nothing(monkeypatch):
    monkeypatch.setattr("src.model_context._configured_endpoint_kind", lambda url: "api")
    payload = {}
    llm_core._apply_local_qwen_thinking_mode(
        payload, "https://api.example.com/v1", "qwen3.5-9b", "off")
    assert payload == {}
