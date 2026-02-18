from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, List

from sqlalchemy import create_engine, text

from app.db.engine import get_database_url


def _read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _split_sql(sql: str) -> List[str]:
    parts = [p.strip() for p in sql.split(";")]
    return [p for p in parts if p]


def _print_rows(cols: List[str], rows: List[Any], limit: int) -> None:
    show = rows if limit == 0 else rows[:limit]
    print("COLUMNS:", cols)
    for r in show:
        print(tuple(r))
    if limit > 0 and len(rows) > limit:
        print(f"... ({len(rows) - limit} filas mas)")


def cmd_db_validate(args: argparse.Namespace) -> int:
    sql_path = Path(args.sql)
    if not sql_path.exists():
        raise SystemExit(f"[ERROR] SQL no existe: {sql_path}")

    url = args.db_url or get_database_url()
    engine = create_engine(url, future=True)

    with engine.connect() as conn:
        for stmt in _split_sql(_read_sql(sql_path)):
            if not stmt:
                continue
            result = conn.execute(text(stmt))
            rows = result.fetchall()
            cols = list(result.keys())
            print(f"\n--- RESULT ({len(rows)} filas) ---")
            _print_rows(cols, rows, limit=args.limit)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Ejecuta queries de validacion (conteos/integridad)")
    ap.add_argument("--db-url", default="", help="DATABASE_URL override (opcional)")
    ap.add_argument("--sql", default="app/db/migrations/validation.sql", help="Ruta al SQL de validacion")
    ap.add_argument("--limit", type=int, default=200, help="Limite de filas a imprimir (0 = sin limite)")
    args = ap.parse_args()
    return cmd_db_validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
