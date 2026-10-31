# Shim: moved to fork/beliefs/move.py; this old path is the same module object.
import sys

from fork.beliefs import move as _module

sys.modules[__name__] = _module
