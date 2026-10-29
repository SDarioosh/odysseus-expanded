# Shim: moved to fork/athena/voice_enroll_routes.py; this old path is the same module object.
import sys

from fork.athena import voice_enroll_routes as _module

sys.modules[__name__] = _module
