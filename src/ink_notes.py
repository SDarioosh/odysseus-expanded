# Shim: moved to fork/ink/notes.py; this old path is the same module object.
import sys

from fork.ink import notes as _module

sys.modules[__name__] = _module
