# Shim: moved to fork/athena/routes.py; this old path is the same module object.
import sys

from fork.athena import routes as _module

sys.modules[__name__] = _module
