# Shim: moved to fork/insight/trends.py; this old path is the same module object.
import sys

from fork.insight import trends as _module

sys.modules[__name__] = _module
