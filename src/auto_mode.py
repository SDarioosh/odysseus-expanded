# Shim: moved to fork/routing/auto_mode.py; this old path is the same module object.
import sys

from fork.routing import auto_mode as _module

sys.modules[__name__] = _module
