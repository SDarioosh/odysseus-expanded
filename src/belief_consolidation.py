# Shim: moved to fork/beliefs/consolidation.py; this old path is the same module object.
import sys

from fork.beliefs import consolidation as _module

sys.modules[__name__] = _module
