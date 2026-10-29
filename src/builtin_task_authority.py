# Shim: moved to fork/hub/builtin_task_authority.py; this old path is the same module object.
import sys

from fork.hub import builtin_task_authority as _module

sys.modules[__name__] = _module
