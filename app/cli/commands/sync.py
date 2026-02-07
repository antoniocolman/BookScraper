from __future__ import annotations
import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Set

from app.db.tools import _get_db, _map_site_row_to_db
from app.services.sites_registry import _get_site, _safe_run_from_file, _safe_run_single
from app.utils.common import (
    _stderr, _is_isbnish, _read_csv_as_dicts, _row_to_dict,
    coerce_rows, load_isbns_from_query_file, normalize_isbn,
    resolve_query_file, write_csv,
)

def cmd_sync(args: argparse.Namespace) -> int:
    
    site_params = {}
    sp = (getattr(args, "site_params_file", "") or "").strip()
    if sp and Path(sp).exists():
        site_params = json.loads(Path(sp).read_text(encoding="utf-8"))
    default_p = site_params.get("_default", {})
    
    if not (args.query or args.query_file):
        raise SystemExit("[ERROR] Debés usar --query o --query-file")

    if args.query:
        raw_isbns = [normalize_isbn(args.query)]
    else:
        qf = resolve_query_file(args.query_file)
        raw_isbns = load_isbns_from_query_file(qf, limit=args.limit_queries)

    isbns = [x for x in raw_isbns if _is_isbnish(x)]
    if not isbns:
        raise SystemExit("[ERROR] No se encontró ningún ISBN válido para sync.")

    db = _get_db(args.db_path)

    present = db.isbns_present(isbns)
    missing = [x for x in isbns if x not in present]
    print(f"[SYNC] Total: {len(isbns)} | en DB: {len(present)} | faltan: {len(missing)}")

    if not missing and getattr(args, "remaining_out", ""):
        outp = Path(args.remaining_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text("", encoding="utf-8")
        print(f"[SYNC] remaining -> {outp} (0)")

    sites: List[str] = []
    for s in (args.sites or []):
        for part in str(s).split(","):
            part = part.strip()
            if part:
                sites.append(part)
 
    seen: Set[str] = set()
    sites = [s for s in sites if not (s in seen or seen.add(s))]

    if missing and not sites:
        raise SystemExit("[ERROR] Hay ISBNs faltantes pero no pasaste --sites para scrappear.")
    scraped_rows: List[Dict[str, Any]] = []
    remaining: List[str] = []

    if missing:
        remaining = list(missing)

        for site_id in sites:
            if not remaining:
                break

            site = _get_site(site_id)
            caps = set(site.capabilities or ())

            # Params efectivos por sitio (desde site_params.json)
            eff: Dict[str, Any] = {}
            eff.update(default_p or {})
            eff.update((site_params or {}).get(site_id, {}))

            eff_max_results = int(eff.get("max_results", getattr(args, "max_results", 4)) or 4)
            eff_delay = float(eff.get("delay", getattr(args, "delay", 2.0)) or 0.0)
            eff_query_delay = float(eff.get("query_delay", getattr(args, "query_delay", 1.0)) or 0.0)
            eff_batch_size = int(eff.get("batch_size", getattr(args, "batch_size", 0)) or 0)
            eff_batch_pause = float(eff.get("batch_pause", getattr(args, "batch_pause", 0.0)) or 0.0)

            # Si no soporta ni query ni query-file, saltamos
            if ("query" not in caps) and ("query-file" not in caps):
                print(f"[SYNC] {site_id}: no soporta 'query' ni 'query-file' (saltando)")
                continue

            # --- BATCH REAL: si batch_size > 0 y el sitio soporta run_from_file ---
            use_batch = (eff_batch_size > 0) and ("query-file" in caps) and callable(getattr(site, "run_from_file", None))

            if use_batch:

                tmp_dir = Path(tempfile.mkdtemp(prefix=f"bsync_{site_id}_"))
                qpath = tmp_dir / "queries.txt"
                outcsv = tmp_dir / "out.csv"

                qpath.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                print(f"[SYNC] {site_id}: batch run_from_file n={len(remaining)} batch={eff_batch_size} pause={eff_batch_pause}")

                try:
                    ret = _safe_run_from_file(
                        site,
                        str(qpath),
                        max_results=eff_max_results,
                        delay=eff_delay,
                        query_delay=eff_query_delay,
                        batch_size=eff_batch_size,
                        batch_pause=eff_batch_pause,
                        limit_queries=len(remaining),
                        output=str(outcsv),  # si el sitio soporta output, lo usará; si no, se ignora
                    )

                    # Obtener filas: preferimos lo retornado; si no retorna nada pero escribió out.csv, lo leemos
                    norm_rows = coerce_rows(ret)
                    if not norm_rows and outcsv.exists():
                        norm_rows = _read_csv_as_dicts(outcsv)

                    # Registrar filas + detectar ISBN encontrados
                    found: Set[str] = set()
                    for d in coerce_rows(norm_rows):
                        d.setdefault("sitio", site_id)
                        d.setdefault("_src_site", site_id)
                        scraped_rows.append(d)

                        cand = (
                            d.get("ISBN") or d.get("isbn") or d.get("Isbn")
                            or d.get("ean") or d.get("gtin13")
                        )
                        if cand:
                            n = normalize_isbn(str(cand))
                            if n:
                                found.add(n)

                    # Reducir remaining según lo encontrado
                    if found:
                        remaining = [x for x in remaining if normalize_isbn(x) not in found]

                except Exception as e:
                    _stderr(f"[SYNC] {site_id}: error batch: {e}")

            else:
                # --- MODO NORMAL: run_single por ISBN ---
                if "query" not in caps:
                    print(f"[SYNC] {site_id}: no soporta 'query' (saltando)")
                    continue

                still_missing: List[str] = []
                for isbn in remaining:
                    try:
                        rows = _safe_run_single(
                            site,
                            isbn,
                            max_results=eff_max_results,
                            delay=eff_delay,
                            query_delay=eff_query_delay,
                            batch_size=eff_batch_size,
                            batch_pause=eff_batch_pause,
                            fetch_detail=getattr(args, "fetch_detail", None),
                            limit_queries=1,
                        )
                        norm_rows = coerce_rows(rows)
                        if norm_rows:
                            d = norm_rows[0]  # mantenemos 1ra fila
                            d.setdefault("sitio", site_id)
                            d.setdefault("_src_site", site_id)
                            scraped_rows.append(d)
                        else:
                            still_missing.append(isbn)
                    except Exception as e:
                        _stderr(f"[SYNC] {site_id}: error en {isbn}: {e}")
                        still_missing.append(isbn)

                remaining = still_missing

            if remaining:
                print(f"[SYNC] Luego de {site_id}: siguen faltando {len(remaining)} ISBN(s).")

        if remaining:
            print(f"[SYNC] No se pudieron obtener {len(remaining)} ISBN(s): {', '.join(remaining[:10])}{'...' if len(remaining)>10 else ''}")

        if getattr(args, "remaining_out", ""):
            outp = Path(args.remaining_out)
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
            print(f"[SYNC] remaining -> {outp} ({len(remaining)})")

        payload = [_map_site_row_to_db(r, sitio=(r.get("sitio") or r.get("_src_site") or "")) for r in scraped_rows]
        if hasattr(db, "upsert_many"):
            inserted, updated = db.upsert_many(payload)
        elif hasattr(db, "upsert_batch"):
            inserted, updated = db.upsert_batch(payload)
        else:
            raise AttributeError("Database no tiene upsert_many ni upsert_batch")
        print(f"[DB] upsert: {inserted} insertados, {updated} actualizados")

    export_rows = db.fetch_by_isbns(isbns)
    db.close()

    if args.output:
        out_path = Path(args.output)
        write_csv([_row_to_dict(r) for r in export_rows], out_path)
        print(f"[EXPORT] {len(export_rows)} filas -> {out_path}")
    else:
        print(json.dumps(export_rows[:5], ensure_ascii=False, indent=2))
        if len(export_rows) > 5:
            print(f"... ({len(export_rows)} filas exportables)")

    return 0
