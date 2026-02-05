#!/usr/bin/env python3
"""Apply a SQL file (executescript) to the project SQLite DB.

Usage:
  python .\tools\apply_sql.py
  python .\tools\apply_sql.py --sql app\sql\book_master.sql --db data\booksearchv2.db
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def _list_views(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "select name from sqlite_master where type='view' order by name"
    ).fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Aplica un archivo .sql a la DB SQLite.")
    ap.add_argument("--sql", default="app/sql/book_master.sql", help="Ruta al .sql")
    ap.add_argument("--db", default="data/booksearchv2.db", help="Ruta a la DB .db")
    args = ap.parse_args()

    sql_path = Path(args.sql)
    db_path = Path(args.db)

    if not sql_path.exists():
        raise SystemExit(f"[ERR] No existe el SQL: {sql_path}")
    if not db_path.exists():
        raise SystemExit(f"[ERR] No existe la DB:  {db_path}")

    # utf-8-sig tolera BOM (común si se guardó desde Windows/VSCode)
    sql_text = sql_path.read_text(encoding="utf-8-sig")

    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(sql_text)
        con.commit()
        print(f"[OK] SQL aplicado -> {sql_path} -> {db_path}")

        views = _list_views(con)
        if views:
            print("[OK] VIEWS:", ", ".join(views))
        else:
            print("[WARN] No hay views en sqlite_master (type='view').")

        for view in ("book_isbn_site", "book_best_std", "book_master"):
            if view not in views:
                print(f"[WARN] View no existe: {view}")
                continue
            try:
                n = con.execute(f"select count(*) from {view}").fetchone()[0]
                print(f"[OK] {view}: {n} filas")
            except Exception as e:
                print(f"[WARN] No pude contar view {view}: {e}")
    finally:
        con.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())