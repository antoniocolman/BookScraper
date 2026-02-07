from __future__ import annotations

# Compat shim: mantiene el módulo app.sites.yenny_search para el registry/CLI,
# pero la implementación vive en app.sites.yenny.wrapper

from app.sites.yenny.wrapper import (  # noqa: F401
    SITE_ID,
    SITE_NAME,
    CAPABILITIES,
    DEFAULT_OUTPUT,
    run_single,
    run_from_file,
)

__all__ = [
    "SITE_ID",
    "SITE_NAME",
    "CAPABILITIES",
    "DEFAULT_OUTPUT",
    "run_single",
    "run_from_file",
]
