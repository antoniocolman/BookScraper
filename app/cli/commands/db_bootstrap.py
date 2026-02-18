from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from sqlalchemy import create_engine, text

from app.db.engine import get_database_url


def _read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _split_sql(sql: str) -> List[str]:
    parts = [p.strip() for p in sql.split(";")]
    return [p for p in parts if p]


def cmd_db_bootstrap(args: argparse.Namespace) -> int:
    sql_path = Path(args.sql)
    if not sql_path.exists():
        raise SystemExit(f"[ERROR] SQL no existe: {sql_path}")

    url = args.db_url or get_database_url()
    engine = create_engine(url, future=True)

    with engine.connect() as conn:
        for stmt in _split_sql(_read_sql(sql_path)):
            conn.execute(text(stmt))
        conn.commit()

    print(f"[OK] Bootstrap aplicado: {sql_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Aplica bootstrap SQL (schema + views)")
    ap.add_argument("--db-url", default="", help="DATABASE_URL override (opcional)")
    ap.add_argument("--sql", default="app/db/migrations/bootstrap.sql", help="Ruta al SQL idempotente")
    args = ap.parse_args()
    return cmd_db_bootstrap(args)


if __name__ == "__main__":
    raise SystemExit(main())
