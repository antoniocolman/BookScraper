# tools/ingest_amazon_json.py
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from app.storage.book_std_db import Database


# --- helpers ---
MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

def normalize_isbn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", (s or "")).upper()

def parse_date_to_iso(s: str) -> str:
    """
    Soporta: '16 Mayo 2023', '10 Octubre 2023', 'May 16 2023', 'May 16, 2023'
    Devuelve YYYY-MM-DD si puede, si no devuelve el string original.
    """
    if not s:
        return ""
    raw = str(s).strip()

    # ES: "16 Mayo 2023"
    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+(\d{4})\s*$", raw)
    if m:
        d = int(m.group(1))
        mon_txt = m.group(2).lower().replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u")
        y = int(m.group(3))
        mon = MONTHS_ES.get(mon_txt) or MONTHS_EN.get(mon_txt)
        if mon:
            return f"{y:04d}-{mon:02d}-{d:02d}"

    # EN: "May 16, 2023" o "May 16 2023"
    m = re.match(r"^\s*([A-Za-z]+)\s+(\d{1,2})[,]?\s+(\d{4})\s*$", raw)
    if m:
        mon_txt = m.group(1).lower()
        d = int(m.group(2))
        y = int(m.group(3))
        mon = MONTHS_EN.get(mon_txt)
        if mon:
            return f"{y:04d}-{mon:02d}-{d:02d}"

    return raw  # no pude parsear

def iter_json_files(inp: str) -> List[Path]:
    # glob pattern
    if any(ch in inp for ch in ["*", "?", "["]):
        return [Path(p) for p in glob.glob(inp)]
    p = Path(inp)
    if p.is_dir():
        return sorted(p.rglob("*.json"))
    return [p]

def load_rows_from_file(fp: Path) -> List[Dict[str, Any]]:
    data = json.loads(fp.read_text(encoding="utf-8", errors="ignore"))
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []

def coerce_row(r: Dict[str, Any]) -> Dict[str, Any]:
    rr = dict(r)

    # normalizar SITE para que quede alineado con tu CLI "amazon_books"
    site = (rr.get("SITE") or rr.get("site") or "").strip()
    if site.lower() == "amazon":
        rr["SITE"] = "amazon_books"
    elif site:
        rr["SITE"] = site

    # normalizar ISBN desde campos principales o bullets si faltan
    rr["ISBN"] = normalize_isbn(rr.get("ISBN", "") or "")
    rr["ISBN10"] = normalize_isbn(rr.get("ISBN10", "") or "")

    bullets = rr.get("_amazon_detail_kv") or {}
    if isinstance(bullets, dict):
        if not rr["ISBN"]:
            for k, v in bullets.items():
                if "ISBN-13" in str(k):
                    rr["ISBN"] = normalize_isbn(str(v))
                    break
        if not rr["ISBN10"]:
            for k, v in bullets.items():
                if "ISBN-10" in str(k):
                    rr["ISBN10"] = normalize_isbn(str(v))
                    break

    # si ISBN está vacío pero ISBN10 existe, lo dejamos igual (tu DB acepta ambos como texto)
    # fecha -> ISO si se puede
    if "FECHA PUBLICACION" in rr and rr["FECHA PUBLICACION"]:
        rr["FECHA PUBLICACION"] = parse_date_to_iso(str(rr["FECHA PUBLICACION"]))

    return rr

def main() -> int:
    ap = argparse.ArgumentParser(description="Ingesta JSON de extensión Amazon a book_std (SQLite).")
    ap.add_argument("--input", required=True, help="Archivo .json, carpeta, o glob (ej: .\\Downloads\\amazon_*.json)")
    ap.add_argument("--db-path", default=r".\data\booksearchv2.db", help="Ruta DB SQLite")
    args = ap.parse_args()

    files = iter_json_files(args.input)
    if not files:
        print(f"[FAIL] No encontré archivos para: {args.input}")
        return 2

    rows: List[Dict[str, Any]] = []
    for fp in files:
        if not fp.exists():
            continue
        for r in load_rows_from_file(fp):
            rr = coerce_row(r)
            # mínimo para PK y upsert
            if rr.get("SITE") and rr.get("URL"):
                rows.append(rr)

    if not rows:
        print("[FAIL] No hay filas válidas (necesito al menos SITE + URL).")
        return 3

    db = Database(args.db_path)
    try:
        ins, upd = db.upsert_books(rows)
    finally:
        db.close()

    print(f"[OK] Ingesta completada -> {len(rows)} filas procesadas | insertados={ins} | actualizados={upd}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())