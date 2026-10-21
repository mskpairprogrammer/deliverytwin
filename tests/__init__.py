"""Test suite. Run from the repository root: ``python -m unittest discover -s tests -t .``

Importing this package puts `src/` (the deployable app) and the repository root (for `scripts`) on the path,
and keeps the developer's `.env` out of the tests: the app loads `.env` when it is imported, so anything that
file adds to the environment (keys, Foundry/Databricks switches) is removed again before any test runs.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_before = set(os.environ)
import deliverytwin.llm  # noqa: E402  (loads the .env files)

for _key in set(os.environ) - _before:
    del os.environ[_key]
