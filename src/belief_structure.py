# Shim: moved to fork/beliefs/structure.py; this old path is the same module object.
import sys

from fork.beliefs import structure as _module

sys.modules[__name__] = _module
