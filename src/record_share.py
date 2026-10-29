# Shim: moved to fork/athena/record_share.py; this old path is the same module object.
import sys

from fork.athena import record_share as _module

sys.modules[__name__] = _module
