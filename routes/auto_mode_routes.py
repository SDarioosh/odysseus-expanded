# Shim: moved to fork/routes/auto_mode_routes.py; this old path is the same module object.
import sys

from fork.routes import auto_mode_routes as _module

sys.modules[__name__] = _module
