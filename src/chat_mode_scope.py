# Shim: moved to fork/routing/chat_mode_scope.py; this old path is the same module object.
import sys

from fork.routing import chat_mode_scope as _module

sys.modules[__name__] = _module
