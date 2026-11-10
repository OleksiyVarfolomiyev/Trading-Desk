"""Start the live desk dashboard from any folder:

    /Users/admin/anaconda3/envs/py314/bin/python /Users/admin/Dropbox/Investing/trading-desk/run_dashboard.py --open

Same options as ``python -m desk.dashboard`` (run from the repository root).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from desk.dashboard import main  # noqa: E402

if __name__ == "__main__":
    from desk.privacy import mask_output

    mask_output()  # only the last four characters of any wallet or account number are shown
    raise SystemExit(main())
