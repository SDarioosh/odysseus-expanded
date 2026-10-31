# Shim: moved to fork/ink/notebooks.py; this old path is the same module object.
import sys

from fork.ink import notebooks as _module

sys.modules[__name__] = _module
