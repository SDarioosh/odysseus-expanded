# Shim: moved to fork/insight/outcomes.py; this old path is the same module object.
import sys

from fork.insight import outcomes as _module

sys.modules[__name__] = _module
