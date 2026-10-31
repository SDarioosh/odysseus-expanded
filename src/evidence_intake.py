# Shim: moved to fork/beliefs/evidence_intake.py; this old path is the same module object.
import sys

from fork.beliefs import evidence_intake as _module

sys.modules[__name__] = _module
