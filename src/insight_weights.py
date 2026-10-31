# Shim: moved to fork/insight/weights.py; this old path is the same module object.
import sys

from fork.insight import weights as _module

sys.modules[__name__] = _module
