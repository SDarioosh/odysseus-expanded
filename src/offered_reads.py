# Shim: moved to fork/routing/offered_reads.py; this old path is the same module object.
import sys

from fork.routing import offered_reads as _module

sys.modules[__name__] = _module
