from __future__ import annotations
import argparse
from pathlib import Path

from app.db.tools import _get_db
from app.utils.common import _row_to_dict, load_isbns_from_query_file, normalize_isbn, resolve_query_file, write_csv

def cmd_export(args: argparse.Namespace) -> int:
    db = _get_db(args.db_path)

    if args.all:
        rows = db.fetch_all()
    else:
        if not (args.query or args.query_file):
            raise SystemExit("[ERROR] export requiere --all o --query/--query-file")
        if args.query:
            isbns = [normalize_isbn(args.query)]
        else:
            qf = resolve_query_file(args.query_file)
            isbns = load_isbns_from_query_file(qf, limit=args.limit_queries)
        rows = db.fetch_by_isbns(isbns)

    db.close()

    out_path = Path(args.output)
    write_csv([_row_to_dict(r) for r in rows], out_path)
    print(f"[EXPORT] {len(rows)} filas -> {out_path}")
    return 0
