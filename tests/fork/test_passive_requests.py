"""A voice device's own polls are passive; turns and owner requests are not."""
import pytest

from fork.hub.passive_requests import is_passive
from src.interactive_gate import should_track_interactive_request

P = "/api/chat/voice/v1"


@pytest.mark.parametrize("method,path", [
    ("GET", P + "/announcements"), ("GET", P + "/turns/latest"), ("GET", P + "/alarms"),
    ("GET", P + "/devices/echo-1/settings"), ("GET", P + "/app/latest"),
    ("POST", P + "/satellite/hello"), ("POST", P + "/satellite/timing"),
    ("GET", P + "/announcements/"),
])
def test_device_polls_are_passive(method, path):
    assert is_passive(path, method)
    assert not should_track_interactive_request(path, method)


@pytest.mark.parametrize("method,path", [
    ("POST", P + "/satellite/turn"), ("POST", P + "/chat/completions"),
    ("POST", P + "/satellite/interrupt"), ("POST", P + "/todos/done"),
    ("DELETE", P + "/alarms/al_1"), ("PUT", "/api/fork/voice/devices/echo-1/settings"),
    ("POST", P + "/announcements"), ("GET", P + "/devices/a/b/settings"),
    ("GET", "/api/news/today"), ("POST", "/api/chat/stream"),
])
def test_turns_and_owner_requests_stay_foreground(method, path):
    assert not is_passive(path, method)
    assert should_track_interactive_request(path, method)
