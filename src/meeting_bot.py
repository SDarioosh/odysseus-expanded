# Shim: moved to fork/athena/meeting_bot.py; this old path is the same module object.
import sys

from fork.athena import meeting_bot as _module

sys.modules[__name__] = _module
