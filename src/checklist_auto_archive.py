# Shim: moved to fork/hub/checklist_auto_archive.py; this old path is the same module object.
import sys

from fork.hub import checklist_auto_archive as _module

sys.modules[__name__] = _module
