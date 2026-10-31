# Shim: moved to fork/routes/today_insight_routes.py; this old path is the same module object.
import sys

from fork.routes import today_insight_routes as _module

sys.modules[__name__] = _module
