# Shim: moved to fork/routing/skill_mcp_toolsets.py; this old path is the same module object.
import sys

from fork.routing import skill_mcp_toolsets as _module

sys.modules[__name__] = _module
