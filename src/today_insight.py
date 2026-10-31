# Shim: moved to fork/insight/today.py; this old path is the same module object.
import sys

from fork.insight import today as _module

sys.modules[__name__] = _module
