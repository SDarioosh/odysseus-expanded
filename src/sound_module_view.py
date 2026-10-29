# Shim: moved to fork/athena/view.py; this old path is the same module object.
import sys

from fork.athena import view as _module

sys.modules[__name__] = _module
