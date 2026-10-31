# Shim: moved to fork/routes/ink_routes.py; this old path is the same module object.
import sys

from fork.routes import ink_routes as _module

sys.modules[__name__] = _module
