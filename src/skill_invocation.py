# Shim: moved to fork/routing/skill_invocation.py; this old path is the same module object.
import sys

from fork.routing import skill_invocation as _module

sys.modules[__name__] = _module
