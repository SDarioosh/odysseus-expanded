# Shim: moved to fork/athena/facts.py; this old path is the same module object.
import sys

from fork.athena import facts as _module

sys.modules[__name__] = _module
