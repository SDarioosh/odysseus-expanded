# Shim: moved to fork/athena/phone_pairing.py; this old path is the same module object.
import sys

from fork.athena import phone_pairing as _module

sys.modules[__name__] = _module
