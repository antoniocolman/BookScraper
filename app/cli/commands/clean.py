from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from typing import Set

from app.utils.common import strip_html

def cmd_clean(args: argparse.Namespace) -> int:
    inp = Path(args.input)
    if not inp.exists():
        raise SystemExit(f"[ERROR] No existe input: {inp}")
    outp = Path(args.output) if args.output else inp.with_name(inp.stem + "_clean.csv")

    with inp.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    strip_cols = []
    if args.strip_html_cols:
        strip_cols = [c.strip() for c in args.strip_html_cols.split(",") if c.strip()]

    flatten_col = args.flatten_json_col.strip() if args.flatten_json_col else ""

    extra_keys: Set[str] = set()
    for r in rows:
        for c in strip_cols:
            if c in r and r[c]:
                r[c] = strip_html(r[c])
        if flatten_col and flatten_col in r and r[flatten_col]:
            try:
                d = json.loads(r[flatten_col])
                if isinstance(d, dict):
                    for k, v in d.items():
                        kk = f"{flatten_col}__{k}"
                        if kk not in r:
                            r[kk] = v
                            extra_keys.add(kk)
            except Exception:
                pass

    out_fields = list(fieldnames)
    for k in sorted(extra_keys):
        if k not in out_fields:
            out_fields.append(k)

    with outp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"[CLEAN] {len(rows)} filas -> {outp}")
    return 0