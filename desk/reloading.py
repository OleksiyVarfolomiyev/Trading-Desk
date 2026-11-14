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


PASSES = 3


def reload_desk(*extra: ModuleType) -> list[str]:
    """Reload all loaded desk modules, then ``extra`` in the order given; return the names reloaded.

    A module reloaded before something it imports (``desk.hl`` sorts before ``desk.numbers``) can fail to find
    a name the new code of that module added. It is not an error yet: the module is retried on the next pass,
    by which time everything else has new code. Only a failure that survives the last pass is raised.
    """
    names = sorted(name for name, module in list(sys.modules.items())
                   if module is not None and (name == "desk" or name.startswith("desk.")) and name != __name__)
    failed: dict[str, Exception] = {}
    for _ in range(PASSES):
        failed = {}
        for module in [sys.modules.get(name) for name in names] + list(extra):
            if module is None:
                continue
            try:
                importlib.reload(module)
            except (ImportError, AttributeError) as error:   # a name the not yet reloaded module lacks
                failed[module.__name__] = error
        if not failed:
            break
    if failed:
        name, error = next(iter(failed.items()))
        raise ImportError(f"{name} could not be reloaded: {error}") from error
    return names + [module.__name__ for module in extra]
