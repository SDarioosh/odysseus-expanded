# Shim: moved to fork/routing/personal_store_escalation.py; this old path is the same module object.
import sys

from fork.routing import personal_store_escalation as _module

sys.modules[__name__] = _module
