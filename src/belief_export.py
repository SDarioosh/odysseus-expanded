# Shim: moved to fork/beliefs/export.py; this old path is the same module object.
import sys

from fork.beliefs import export as _module

sys.modules[__name__] = _module
