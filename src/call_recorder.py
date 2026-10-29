# Shim: moved to fork/athena/call_recorder.py; this old path is the same module object.
import sys

from fork.athena import call_recorder as _module

sys.modules[__name__] = _module
