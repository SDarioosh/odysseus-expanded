"""``fork.routes.mount``: the one ``app.py`` hook that includes the fork's routers."""
from __future__ import annotations

from pathlib import Path

from fork import routes

ROOT = Path(__file__).resolve().parents[2]

# the order app.py included them in before the hooks became one call
ORDER = [
    "setup_sound_module_routes",
    "setup_today_insight_routes",
    "setup_belief_search_routes",
    "setup_beliefs_routes",
    "setup_sound_relay_routes",
    "setup_summary_model_routes",
    "setup_whisper_vocabulary_routes",
    "setup_voice_enroll_routes",
    "setup_auto_mode_routes",
    "setup_ink_routes",
]


def test_routers_in_the_old_order():
    assert [factory for _, factory in routes.ROUTERS] == ORDER


def test_mount_includes_each_router_once_in_order(monkeypatch):
    import importlib

    built = []

    def fake_import(name):
        factory = dict(routes.ROUTERS)[name]
        return type("M", (), {factory: staticmethod(lambda f=factory: built.append(f) or f)})

    monkeypatch.setattr(importlib, "import_module", fake_import)

    class App:
        def __init__(self):
            self.included = []

        def include_router(self, router):
            self.included.append(router)

    app = App()
    routes.mount(app)
    assert app.included == built == ORDER


def test_app_mounts_after_the_mcp_routes():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    hook = app.index("_fork_mount_routes(app)")
    assert app.index("app.include_router(setup_mcp_routes(mcp_manager))") < hook
    assert hook < app.index('logger.info("MCP routes initialized")')
    assert app.count("_fork_mount_routes(app)") == 1
