# Shim: moved to fork/routes/insight_outcome_routes.py; this old path is the same module object.
import sys

from fork.routes import insight_outcome_routes as _module

sys.modules[__name__] = _module
