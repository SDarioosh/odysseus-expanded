# Shim: moved to fork/ink/athena.py; this old path is the same module object.
import sys

from fork.ink import athena as _module

sys.modules[__name__] = _module
