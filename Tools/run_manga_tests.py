"""Explicit main entry for Windows spawn-based model regression tests.

Keep Qt and backend test invocations in separate processes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    import multiprocessing

    import pytest

    multiprocessing.freeze_support()
    raise SystemExit(pytest.main(sys.argv[1:]))
