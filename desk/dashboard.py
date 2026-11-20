"""The desk app moved to ``desk/app.py`` (``python -m desk.app``); this stub keeps ``python -m desk.dashboard`` and
``from desk.dashboard import ...`` working for running processes and older notebooks. It can be deleted once nothing uses it."""

import sys

from desk import app

if __name__ == "__main__":
    print("desk.dashboard is now desk.app: starting it (use python -m desk.app).", flush=True)
    raise SystemExit(app.main())

sys.modules[__name__] = app   # `from desk.dashboard import table` resolves in desk.app
