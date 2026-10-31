# Shim: moved to fork/routing/module_turn_scope.py; this old path is the same module object.
import sys

from fork.routing import module_turn_scope as _module

sys.modules[__name__] = _module
