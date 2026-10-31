# Shim: moved to fork/hub/log_redact.py; this old path is the same module object.
import sys

from fork.hub import log_redact as _module

sys.modules[__name__] = _module
