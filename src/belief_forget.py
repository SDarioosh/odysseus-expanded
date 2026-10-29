# Shim: moved to fork/beliefs/forget.py; this old path is the same module object.
import sys

from fork.beliefs import forget as _module

sys.modules[__name__] = _module
