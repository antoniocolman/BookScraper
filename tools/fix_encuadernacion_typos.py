import argparse
import json
import sqlite3

MAP = {
    "Rstica": "Rústica",
    "rstica": "Rústica",
}

def fix_json(raw_json: str) -> str:
    if not raw_json:
        return raw_json
    try:
        obj = json.loads(raw_json)
    except Exception:
        return raw_json

    # Solo tocamos "std" (no "raw", para no “inventar” el origen)
    if isinstance(obj, dict) and isinstance(obj.get("std"), dict):
        std = obj["std"]
        v = std.get("ENCUADERNACION")
        if isinstance(v, str) and v in MAP:
            std["ENCUADERNACION"] = MAP[v]
    return json.dumps(obj, ensure_ascii=False, default=str)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--site", default=None, help="opcional: filtrar por site")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    where = "encuadernacion IN ('Rstica','rstica')"
    params = []
    if args.site:
        where += " AND site=?"
        params.append(args.site)

    rows = cur.execute(
        f"SELECT site, url, encuadernacion, raw_json FROM book_std WHERE {where}",
        params
    ).fetchall()

    print("[INFO] filas a revisar:", len(rows))

    changed = 0
    for site, url, enc, raw_json in rows:
        new_enc = MAP.get(enc, enc)
        new_raw = fix_json(raw_json)

        if new_enc == enc and new_raw == raw_json:
            continue

        changed += 1
        if not args.dry_run:
            cur.execute(
                "UPDATE book_std SET encuadernacion=?, raw_json=?, updated_at=datetime('now') WHERE site=? AND url=?",
                (new_enc, new_raw, site, url)
            )

    if args.dry_run:
        print("[INFO] dry-run: cambios detectados:", changed)
    else:
        con.commit()
        print("[OK] filas actualizadas:", changed)

    con.close()

if __name__ == "__main__":
    main()