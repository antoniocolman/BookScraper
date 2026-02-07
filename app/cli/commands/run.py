from __future__ import annotations
import argparse
import json
from pathlib import Path

from app.db.tools import _get_db, _map_site_row_to_db
from app.services.sites_registry import _get_site, _safe_call
from app.utils.common import coerce_rows, resolve_query_file, write_csv

def cmd_run(args: argparse.Namespace) -> int:
    site = _get_site(args.site)

    kwargs = dict(
        max_results=args.max_results,
        delay=args.delay,
        query_delay=args.query_delay,
        batch_size=args.batch_size,
        batch_pause=args.batch_pause,
        limit_queries=(args.limit_queries if args.limit_queries and args.limit_queries > 0 else None),
        fetch_detail=getattr(args, "fetch_detail", None),  # <-- ESTA
    )

    if args.query:
        raw = _safe_call(site.run_single, args.query, **kwargs)
    elif args.query_file:
        qf = resolve_query_file(args.query_file)
        raw = _safe_call(site.run_from_file, str(qf), **kwargs)
    else:
        raise SystemExit("[ERROR] Debés usar --query o --query-file")

    out_rows = coerce_rows(raw)

    if args.output:
        out_path = Path(args.output)
        print(f"[INFO] Escribiendo CSV en {out_path}")
        write_csv(out_rows, out_path)
        print(f"[CSV] {len(out_rows)} filas escritas en {out_path}")
        print(f"[OK] Finalizado. Salida: {out_path}")

    if args.write_db:
        db = _get_db(args.db_path)
        payload = [_map_site_row_to_db(r, sitio=args.site) for r in out_rows]
        if hasattr(db, "upsert_many"):
            inserted, updated = db.upsert_many(payload)
        elif hasattr(db, "upsert_batch"):
            inserted, updated = db.upsert_batch(payload)
        else:
            raise AttributeError("Database no tiene upsert_many ni upsert_batch")
        db.close()
        print(f"[DB] upsert: {inserted} insertados, {updated} actualizados en {args.db_path}")

    if not args.output:
        print(json.dumps(out_rows[:5], ensure_ascii=False, indent=2))
        if len(out_rows) > 5:
            print(f"... ({len(out_rows)} filas en total)")

    return 0