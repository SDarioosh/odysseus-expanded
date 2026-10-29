# Shim: moved to fork/insight/routines.py; this old path is the same module object.
import sys

from fork.insight import routines as _module

sys.modules[__name__] = _module
