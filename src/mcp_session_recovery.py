# Shim: moved to fork/mcp/session_recovery.py; this old path is the same module object.
import sys

from fork.mcp import session_recovery as _module

sys.modules[__name__] = _module
