from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Tuple, List

# --- Bootstrap de imports: agregar root del proyecto al sys.path ---
# tools/backfill_std_fields.py -> parents[1] = BookSearchV2 (root)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from app.standard import normalize_encuadernacion
except Exception:
    # Fallback mínimo si el import falla por alguna razón rara
    def normalize_encuadernacion(value: Any) -> str:
        v = str(value).strip() if value is not None else ""
        if not v:
            return ""
        v = v.replace("\u00a0", " ").strip()  # nbsp
        # quitar sufijos tipo "Paperback (8/28/2006)"
        import re
        v2 = re.sub(r"\s*\([^)]*\)\s*", "", v).strip()
        low = v2.lower()

        if any(x in low for x in ["ebook", "e-book", "digital", "kindle"]):
            return "eBook"
        if any(x in low for x in ["hardcover", "hard cover", "hardback", "tapa dura"]):
            return "Tapa Dura"
        if any(x in low for x in ["paperback", "mass market paperback"]):
            return "De Bolsillo"
        if any(x in low for x in ["trade paperback", "tapa blanda", "rustica", "rústica", "softcover", "soft cover"]):
            return "Rústica"
        return v2


def _loads_json(s: str) -> Dict[str, Any]:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _pick(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _derive_from_raw_json(raw: Dict[str, Any]) -> Tuple[str, str]:
    """
    Devuelve (encuadernacion_normalizada, categoria).
    Soporta:
      - raw_json guardado como STD (keys: ENCUADERNACION/CATEGORIA)
      - raw_json guardado como wrapper {raw:{info_adicional:{...}}, std:{...}}
      - raw_json guardado como legacy con info_adicional suelta
    """
    # 1) Si raw_json ya es STD
    enc = _pick(raw, "ENCUADERNACION", "encuadernacion", "binding", "format", "formato", "cover", "tapa")
    cat = _pick(raw, "CATEGORIA", "categoria", "category", "genre", "genero", "género")

    # 2) Si viene como wrapper con raw/std
    raw_block = raw.get("raw")
    std_block = raw.get("std")

    if isinstance(std_block, dict):
        enc = enc or _pick(std_block, "ENCUADERNACION", "encuadernacion", "binding", "format", "formato", "cover", "tapa")
        cat = cat or _pick(std_block, "CATEGORIA", "categoria", "category", "genre", "genero", "género")

    info: Dict[str, Any] = {}
    if isinstance(raw_block, dict):
        info = raw_block.get("info_adicional") or {}
        if not isinstance(info, dict):
            info = {}
    else:
        info = raw.get("info_adicional") or {}
        if not isinstance(info, dict):
            info = {}

    # 3) Info adicional (el_lector/yenny etc)
    enc = enc or _pick(info, "encuadernacion", "binding", "format", "formato", "cover", "tapa")
    cat = cat or _pick(info, "categoria", "category", "genre", "genero", "género")

    enc_norm = normalize_encuadernacion(enc)
    return enc_norm, cat.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill encuadernacion/categoria desde raw_json")
    ap.add_argument("--db", required=True, help="Ruta a sqlite DB")
    ap.add_argument("--site", default="", help="Filtrar por site (opcional)")
    ap.add_argument("--limit", type=int, default=0, help="Limitar filas a procesar (0 = todas)")
    ap.add_argument("--dry-run", action="store_true", help="No escribe cambios, solo muestra conteos")
    ap.add_argument("--only-missing", action="store_true", help="Solo filas con encuadernacion/categoria vacías")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    where = []
    params: List[Any] = []

    if args.site.strip():
        where.append("site = ?")
        params.append(args.site.strip())

    if args.only_missing:
        where.append("(IFNULL(TRIM(encuadernacion),'') = '' OR IFNULL(TRIM(categoria),'') = '')")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f" LIMIT {args.limit}" if args.limit and args.limit > 0 else ""

    q = f"""
    SELECT site, url, encuadernacion, categoria, raw_json
    FROM book_std
    {where_sql}
    ORDER BY updated_at DESC
    {limit_sql}
    """

    rows = cur.execute(q, params).fetchall()

    to_update = []
    changed = 0
    missing_raw = 0

    for site, url, enc_old, cat_old, raw_json in rows:
        enc_old = (enc_old or "").strip()
        cat_old = (cat_old or "").strip()

        raw = _loads_json(raw_json or "")
        if not raw:
            missing_raw += 1
            continue

        enc_new, cat_new = _derive_from_raw_json(raw)

        # No pisar categoría existente con vacío
        if cat_old and not cat_new:
            cat_new = cat_old

        if enc_new != enc_old or cat_new != cat_old:
            changed += 1
            to_update.append((enc_new, cat_new, site, url))

    print(f"[INFO] Filas leídas: {len(rows)}")
    print(f"[INFO] raw_json vacío/ilegible: {missing_raw}")
    print(f"[INFO] Filas con cambios: {changed}")

    if args.dry_run or not to_update:
        print("[INFO] dry-run activo o no hay cambios. No se actualizó nada.")
        con.close()
        return

    cur.executemany(
        """
        UPDATE book_std
        SET encuadernacion = ?,
            categoria = ?,
            updated_at = datetime('now')
        WHERE site = ? AND url = ?
        """,
        to_update,
    )
    con.commit()
    con.close()
    print(f"[OK] Actualizadas: {len(to_update)}")


if __name__ == "__main__":
    main()
