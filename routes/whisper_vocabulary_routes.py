# Shim: moved to fork/athena/whisper_vocabulary_routes.py; this old path is the same module object.
import sys

from fork.athena import whisper_vocabulary_routes as _module

sys.modules[__name__] = _module
