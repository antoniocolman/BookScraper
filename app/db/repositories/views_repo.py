from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Connection


def list_views(conn: Connection) -> List[Tuple[str, str]]:
    name = conn.engine.dialect.name
    if name == "postgresql":
        rows = conn.execute(
            text("SELECT schemaname, viewname, definition FROM pg_views WHERE schemaname = 'public' ORDER BY viewname")
        ).fetchall()
        return [(f"{r[0]}.{r[1]}", r[2]) for r in rows]
    if name == "sqlite":
        rows = conn.execute(text("select name, sql from sqlite_master where type='view' order by name")).fetchall()
        return [(r[0], r[1]) for r in rows]
    raise RuntimeError(f"Unsupported dialect for views: {name}")


def export_view(conn: Connection, view_name: str) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    rows = conn.execute(text(f"select * from {view_name}")).fetchall()
    cols = list(rows[0].keys()) if rows else []
    out_rows = [tuple(r) for r in rows]
    return cols, out_rows
