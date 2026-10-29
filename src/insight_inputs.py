# Shim: moved to fork/insight/inputs.py; this old path is the same module object.
import sys

from fork.insight import inputs as _module

sys.modules[__name__] = _module
