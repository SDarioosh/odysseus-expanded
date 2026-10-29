# Shim: moved to fork/insight/push.py; this old path is the same module object.
import sys

from fork.insight import push as _module

sys.modules[__name__] = _module
