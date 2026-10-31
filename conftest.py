"""Fork: test-run defaults for the whole suite (``tests/`` is upstream's and the fork's).

The brain (``fork/brain/``, ``FORK_BRAIN``) answers chat turns by default. Upstream's
tests and the fork's characterization tests check upstream's own chat path, which is what
runs with ``FORK_BRAIN=off``, so the suite runs with the brain off unless a test turns it
on (the brain's tests do, through ``tests/fork/brain_fakes.py`` and ``monkeypatch``).
A value set in the environment before the run wins.
"""
import os

os.environ.setdefault("FORK_BRAIN", "off")
