# Shim: moved to fork/beliefs/search.py; this old path is the same module object.
import sys

from fork.beliefs import search as _module

sys.modules[__name__] = _module
