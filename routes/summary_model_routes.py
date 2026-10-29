# Shim: moved to fork/athena/summary_model_routes.py; this old path is the same module object.
import sys

from fork.athena import summary_model_routes as _module

sys.modules[__name__] = _module
