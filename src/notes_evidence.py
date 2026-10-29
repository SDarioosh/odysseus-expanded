# Shim: moved to fork/beliefs/notes_evidence.py; this old path is the same module object.
import sys

from fork.beliefs import notes_evidence as _module

sys.modules[__name__] = _module
