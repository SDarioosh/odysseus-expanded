# Shim: moved to fork/athena/relay_routes.py; this old path is the same module object.
import sys

from fork.athena import relay_routes as _module

sys.modules[__name__] = _module
