# Shim: moved to fork/routing/personal_read_grants.py; this old path is the same module object.
import sys

from fork.routing import personal_read_grants as _module

sys.modules[__name__] = _module
