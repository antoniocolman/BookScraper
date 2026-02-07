from __future__ import annotations
import argparse
import csv
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from app.db.tools import iter_qc_issues_from_raw_json
from app.config import EXPORTS_DIR
def cmd_db(args: argparse.Namespace) -> int:
    if not getattr(args, "qc_report", False):
        raise SystemExit("[ERROR] db requiere una acción. Usá --qc-report")

    db_path = args.db_path
    out_path = Path(args.output) if args.output else (EXPORTS_DIR / "qc_report.csv")
    details_path = Path(args.details) if args.details else None
    details_limit = int(args.details_limit or 0)
    site_filter = (args.site or "").strip().lower()
    only_warn = bool(getattr(args, "only_warn", False))
    min_count = int(getattr(args, "min_count", 1) or 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if details_path:
        details_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    sql = "SELECT site, isbn, titulo, url, raw_json FROM book_std WHERE raw_json IS NOT NULL AND raw_json <> ''"
    params: List[Any] = []
    if site_filter:
        sql += " AND lower(site)=?"
        params.append(site_filter)

    cur = con.execute(sql, params)

    counts: Counter = Counter()
    totals: Counter = Counter()
    detail_rows: List[Dict[str, Any]] = []
    detail_written = 0

    for row in cur:
        site = (row["site"] or "").strip()
        isbn = row["isbn"] or ""
        titulo = row["titulo"] or ""
        url = row["url"] or ""
        raw_json_s = row["raw_json"] or ""

        issues = iter_qc_issues_from_raw_json(raw_json_s)
        if not issues:
            continue

        for it in issues:
            code = str(it.get("code") or "").strip() or "UNKNOWN"
            sev = str(it.get("severity") or "").strip() or "info"
            msg = str(it.get("message") or "").strip()

            if only_warn and sev != "warn":
                continue

            counts[(site, code, sev)] += 1
            totals[(site, "__TOTAL__", "__TOTAL__")] += 1

            if details_path and (details_limit <= 0 or detail_written < details_limit):
                detail_rows.append({
                    "SITE": site,
                    "ISBN": isbn,
                    "TITULO": titulo,
                    "URL": url,
                    "CODE": code,
                    "SEVERITY": sev,
                    "MESSAGE": msg,
                })
                detail_written += 1

    con.close()

    report_rows: List[Dict[str, Any]] = []
    for (site, code, sev), c in totals.items():
        report_rows.append({"SITE": site, "CODE": code, "SEVERITY": sev, "COUNT": c})

    for (site, code, sev), c in counts.items():
        if c < min_count:
            continue
        report_rows.append({"SITE": site, "CODE": code, "SEVERITY": sev, "COUNT": c})

    def _sort_key(r):
        is_total = (r["CODE"] == "__TOTAL__")
        return (r["SITE"], 0 if is_total else 1, -int(r["COUNT"]), r["CODE"], r["SEVERITY"])

    report_rows.sort(key=_sort_key)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["SITE", "CODE", "SEVERITY", "COUNT"])
        w.writeheader()
        for r in report_rows:
            w.writerow(r)

    print(f"[QC] Reporte agregado: {len(report_rows)} filas -> {out_path}")

    if details_path:
        with details_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["SITE", "ISBN", "TITULO", "URL", "CODE", "SEVERITY", "MESSAGE"])
            w.writeheader()
            for r in detail_rows:
                w.writerow(r)
        print(f"[QC] Detalle: {len(detail_rows)} filas -> {details_path}")
        if details_limit > 0 and detail_written >= details_limit:
            print(f"[QC] Nota: detalle limitado a {details_limit} filas (--details-limit).")

    return 0
