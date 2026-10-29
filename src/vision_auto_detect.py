# Shim: moved to fork/hub/vision_auto_detect.py; this old path is the same module object.
import sys

from fork.hub import vision_auto_detect as _module

sys.modules[__name__] = _module
