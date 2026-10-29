# Shim: moved to fork/hub/private_endpoint_thinking.py; this old path is the same module object.
import sys

from fork.hub import private_endpoint_thinking as _module

sys.modules[__name__] = _module
