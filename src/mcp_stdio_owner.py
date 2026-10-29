# Shim: moved to fork/mcp/stdio_owner.py; this old path is the same module object.
import sys

from fork.mcp import stdio_owner as _module

sys.modules[__name__] = _module
