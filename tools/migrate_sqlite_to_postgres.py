from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy import create_engine

from app.db.models import book_std


def _read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _split_sql(sql: str) -> List[str]:
    parts = [p.strip() for p in sql.split(";")]
    return [p for p in parts if p]


def _apply_bootstrap(conn: Connection, sql_path: Path) -> None:
    sql_text = _read_sql(sql_path)
    for stmt in _split_sql(sql_text):
        conn.execute(text(stmt))
    conn.commit()


def _iter_sqlite_rows(db_path: Path, batch_size: int) -> Iterable[List[Dict[str, Any]]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute("SELECT * FROM book_std")
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield [dict(r) for r in rows]
    finally:
        con.close()


def _upsert_batch(conn: Connection, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    if conn.engine.dialect.name != "postgresql":
        raise RuntimeError("Target engine must be Postgres for this script")

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    ins = pg_insert(book_std)
    update_cols = {c.name: getattr(ins.excluded, c.name) for c in book_std.c if c.name not in ("site", "url")}
    stmt = ins.on_conflict_do_update(
        index_elements=[book_std.c.site, book_std.c.url],
        set_=update_cols,
    )

    result = conn.execute(stmt, rows)
    conn.commit()
    return result.rowcount or 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate SQLite book_std to Postgres (idempotent).")
    ap.add_argument("--sqlite-path", required=True, help="Ruta a SQLite (booksearchv2.db)")
    ap.add_argument("--postgres-url", required=True, help="Postgres DATABASE_URL")
    ap.add_argument("--bootstrap-sql", default="app/db/migrations/bootstrap.sql", help="SQL idempotente de schema")
    ap.add_argument("--batch-size", type=int, default=1000)
    args = ap.parse_args()

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        raise SystemExit(f"[ERROR] SQLite no existe: {sqlite_path}")

    engine = create_engine(args.postgres_url, future=True)

    with engine.connect() as conn:
        _apply_bootstrap(conn, Path(args.bootstrap_sql))

        total = 0
        for batch in _iter_sqlite_rows(sqlite_path, args.batch_size):
            total += _upsert_batch(conn, batch)
            print(f"[OK] Batch upserted: {len(batch)} (total={total})")

    print(f"[DONE] Migracion completada. Total procesado: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
