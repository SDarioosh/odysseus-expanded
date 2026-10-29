# Shim: moved to fork/ink/transcribe.py; this old path is the same module object.
import sys

from fork.ink import transcribe as _module

sys.modules[__name__] = _module
