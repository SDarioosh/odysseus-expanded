# Shim: moved to fork/athena/meeting_auto_join.py; this old path is the same module object.
import sys

from fork.athena import meeting_auto_join as _module

sys.modules[__name__] = _module
