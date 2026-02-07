from __future__ import annotations

# Compat shim: permite seguir usando:
#   python -m app.sites.experimental.contrapunto_experimental ...
from app.sites.contrapunto.engines.search import *  # noqa

from app.sites.contrapunto.engines.search import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
