# Shim: moved to fork/routing/web_read_recovery.py; this old path is the same module object.
import sys

from fork.routing import web_read_recovery as _module

sys.modules[__name__] = _module
