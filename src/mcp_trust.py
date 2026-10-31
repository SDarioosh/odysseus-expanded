# Shim: moved to fork/mcp/trust.py; this old path is the same module object.
import sys

from fork.mcp import trust as _module

sys.modules[__name__] = _module
