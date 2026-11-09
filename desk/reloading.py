"""Pick up code changes in a running notebook without restarting its kernel.

``reload_desk()`` reloads every loaded ``desk`` module (and any extra modules passed in, such as
the research ones). Modules import names from each other (``from desk.hl.account_analytics import
display_interactive_table``), so reloading only the module a notebook uses leaves it calling
older copies of its helpers; that is how a new keyword argument came to be "unexpected". Two
passes in a fixed order give every module the new code of everything it imports.

Objects created before the reload (an ``analytics`` instance) keep their old class, so the setup
cell that calls this should create them afresh, as every desk notebook's setup cell does.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType


def reload_desk(*extra: ModuleType) -> list[str]:
    """Reload all loaded desk modules, then ``extra`` in the order given; return the names reloaded."""
    names = sorted(name for name, module in list(sys.modules.items())
                   if module is not None and (name == "desk" or name.startswith("desk.")) and name != __name__)
    for _ in range(2):
        for name in names:
            module = sys.modules.get(name)
            if module is not None:
                importlib.reload(module)
        for module in extra:
            importlib.reload(module)
    return names + [module.__name__ for module in extra]
