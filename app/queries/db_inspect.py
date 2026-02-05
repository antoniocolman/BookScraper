# app/db_queries.py
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class QueryDef:
    name: str
    description: str
    sql: str


# ✅ Ajustado a tu tabla real: book_std
BUILTIN_QUERIES: List[QueryDef] = [
    QueryDef(
        "tables",
        "Listar tablas",
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;",
    ),
    QueryDef(
        "schema_book_std",
        "DDL de la tabla book_std",
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='book_std';",
    ),
    QueryDef(
        "book_std_columns",
        "Columnas de book_std (PRAGMA table_info)",
        "PRAGMA table_info(book_std);",
    ),
    QueryDef(
        "count_total",
        "Total de filas (book_std)",
        "SELECT COUNT(*) AS total FROM book_std;",
    ),
    QueryDef(
        "count_by_site",
        "Conteo por site",
        "SELECT site, COUNT(*) AS c FROM book_std GROUP BY site ORDER BY c DESC;",
    ),
    QueryDef(
        "latest",
        "Últimos actualizados (top 50)",
        "SELECT isbn, titulo, site, updated_at FROM book_std ORDER BY updated_at DESC LIMIT 50;",
    ),
    QueryDef(
        "yenny_count",
        "Total filas de Yenny",
        "SELECT COUNT(*) AS yenny_total FROM book_std WHERE site='yenny';",
    ),
    QueryDef(
        "yenny_latest",
        "Últimos actualizados de Yenny (top 50)",
        "SELECT isbn, titulo, autor, site, updated_at FROM book_std WHERE site='yenny' ORDER BY updated_at DESC LIMIT 50;",
    ),
    QueryDef(
        "missing_isbn",
        "Registros sin ISBN",
        "SELECT COUNT(*) AS n FROM book_std WHERE isbn IS NULL OR TRIM(isbn)='';",
    ),
    QueryDef(
        "missing_title",
        "Registros sin título",
        "SELECT COUNT(*) AS n FROM book_std WHERE titulo IS NULL OR TRIM(titulo)='';",
    ),
    QueryDef(
        "missing_author",
        "Registros sin autor",
        "SELECT COUNT(*) AS n FROM book_std WHERE autor IS NULL OR TRIM(autor)='';",
    ),
    QueryDef(
        "missing_synopsis",
        "Registros sin sinopsis",
        "SELECT COUNT(*) AS n FROM book_std WHERE sinopsis IS NULL OR TRIM(sinopsis)='';",
    ),
    QueryDef(
        "dup_isbn",
        "ISBN duplicados (top 100)",
        """
        SELECT isbn, COUNT(*) AS c
        FROM book_std
        WHERE isbn IS NOT NULL AND TRIM(isbn) <> ''
        GROUP BY isbn
        HAVING c > 1
        ORDER BY c DESC
        LIMIT 100;
        """.strip(),
    ),
    QueryDef(
        "dup_isbn_by_site",
        "ISBN duplicados por (site,isbn) (top 100)",
        """
        SELECT site, isbn, COUNT(*) AS c
        FROM book_std
        WHERE isbn IS NOT NULL AND TRIM(isbn) <> ''
        GROUP BY site, isbn
        HAVING c > 1
        ORDER BY c DESC
        LIMIT 100;
        """.strip(),
    ),
]


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _is_readonly_sql(sql: str) -> bool:
    s = (sql or "").strip().lower()
    # Permitimos SELECT / PRAGMA / WITH (CTE)
    return s.startswith("select") or s.startswith("pragma") or s.startswith("with")


def run_sql(
    db_path: str,
    sql: str,
    *,
    params: Optional[Sequence[Any]] = None,
    limit: int = 0,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    if not _is_readonly_sql(sql):
        raise ValueError("Solo se permite SQL de lectura (SELECT / PRAGMA / WITH).")

    con = _connect(db_path)
    try:
        cur = con.execute(sql, params or [])
        rows = cur.fetchall()

        # PRAGMA table_info() devuelve columnas fijas; SELECT devuelve description
        cols = [d[0] for d in (cur.description or [])]  # type: ignore[index]
        if not cols and rows:
            cols = list(rows[0].keys())

        out = [dict(r) for r in rows]
        if limit and limit > 0:
            out = out[:limit]
        return cols, out
    finally:
        con.close()


def list_queries() -> List[Dict[str, str]]:
    return [{"name": q.name, "description": q.description} for q in BUILTIN_QUERIES]


def get_query(name: str) -> QueryDef:
    for q in BUILTIN_QUERIES:
        if q.name == name:
            return q
    raise KeyError(f"Query no existe: {name}")


def print_table(cols: List[str], rows: List[Dict[str, Any]], max_width: int = 60) -> None:
    if not cols:
        print("(sin columnas)")
        return
    if not rows:
        print("(sin filas)")
        return

    # widths
    widths = {c: min(max(len(c), *(len(str(r.get(c, ""))) for r in rows)), max_width) for c in cols}

    def fmt(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        s = str(v)
        s = s.replace("\n", " ").strip()
        if len(s) > max_width:
            s = s[: max_width - 1] + "…"
        return s

    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(fmt(r.get(c, "")).ljust(widths[c]) for c in cols))


def write_csv_file(path: str, cols: List[str], rows: List[Dict[str, Any]]) -> None:
    outp = Path(path)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols or (rows[0].keys() if rows else []))
        w.writeheader()
        for r in rows:
            w.writerow(r)
