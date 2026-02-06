from pathlib import Path
import os
import re
from typing import Final

BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent

# Permite override por env (ideal para Docker/CI)
DATA_DIR: Final[Path] = Path(os.getenv("BOOKSEARCH_DATA_DIR", str(BASE_DIR / "data")))

EXPORTS_DIR: Final[Path] = DATA_DIR / "exports"
QUERIES_DIR: Final[Path] = DATA_DIR / "queries"
LOGS_DIR: Final[Path] = DATA_DIR / "logs"
STATE_DIR: Final[Path] = DATA_DIR / "state"

DB_PATH: Final[Path] = Path(os.getenv("BOOKSEARCH_DB_PATH", str(DATA_DIR / "booksearchv2.db")))
SITE_PARAMS_PATH: Final[Path] = DATA_DIR / "site_params.json"

for _dir in [DATA_DIR, EXPORTS_DIR, QUERIES_DIR, LOGS_DIR, STATE_DIR]:
    _dir.mkdir(parents=True, exist_ok=True)
