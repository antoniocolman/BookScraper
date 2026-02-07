from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "cmd_sites",
    "cmd_site_help",
    "cmd_run",
    "cmd_sync",
    "cmd_export",
    "cmd_clean",
    "cmd_db",
]

# Lazy re-exports to avoid importing heavy deps at package import time.
_EXPORTS = {
    "cmd_sites": ("sites", "cmd_sites"),
    "cmd_site_help": ("sites", "cmd_site_help"),
    "cmd_run": ("run", "cmd_run"),
    "cmd_sync": ("sync", "cmd_sync"),
    "cmd_export": ("export", "cmd_export"),
    "cmd_clean": ("clean", "cmd_clean"),
    "cmd_db": ("db", "cmd_db"),
}


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        mod_name, attr = _EXPORTS[name]
        mod = importlib.import_module(f"{__name__}.{mod_name}")
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")