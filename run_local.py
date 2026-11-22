"""Convenience wrapper: python run_local.py --demo | --live [options]  (see agent/stocknews/local_run.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent"))

from stocknews.local_run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
