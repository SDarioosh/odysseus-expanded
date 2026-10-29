# Shim: moved to fork/routes/belief_search_routes.py; this old path is the same module object.
import sys

from fork.routes import belief_search_routes as _module

sys.modules[__name__] = _module
