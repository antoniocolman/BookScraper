from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.config import DB_PATH


def _default_sqlite_url() -> str:
    # SQLAlchemy expects three slashes for absolute paths on Windows.
    return f"sqlite:///{DB_PATH.as_posix()}"


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", "") or _default_sqlite_url()


@lru_cache(maxsize=1)
def get_engine(echo: bool = False) -> Engine:
    url = get_database_url()
    return create_engine(url, echo=echo, future=True)


def get_connection(echo: bool = False):
    engine = get_engine(echo=echo)
    return engine.connect()
