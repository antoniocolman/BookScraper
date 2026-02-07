from __future__ import annotations

# Compat shim: permite seguir usando
#   python -m app.sites.experimental.yenny_experimental ...
from app.sites.yenny.engines.catalog import *  # noqa

from app.sites.yenny.engines.catalog import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
