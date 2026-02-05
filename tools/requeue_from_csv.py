import argparse
import csv
import re
from pathlib import Path

def norm_isbn(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^0-9Xx]", "", s).upper()

def read_queries(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out

def read_found_isbns(csv_path: Path) -> set[str]:
    found = set()
    if not csv_path.exists():
        return found
    with csv_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return found
        # soporta headers: ISBN / isbn
        isbn_key = None
        for k in reader.fieldnames:
            if k and k.strip().lower() == "isbn":
                isbn_key = k
                break
        if not isbn_key:
            return found

        for row in reader:
            v = norm_isbn(row.get(isbn_key, "") or "")
            if v:
                found.add(v)
    return found

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="archivo chunk con ISBNs/queries")
    ap.add_argument("--csv", required=True, help="CSV exportado por el site (solo encontrados)")
    ap.add_argument("--out", required=True, help="archivo salida con queries no encontradas")
    args = ap.parse_args()

    input_path = Path(args.input)
    csv_path = Path(args.csv)
    out_path = Path(args.out)

    queries = read_queries(input_path)
    found = read_found_isbns(csv_path)

    missing = []
    for q in queries:
        # si es ISBN: lo normalizamos
        nq = norm_isbn(q)
        if nq and nq not in found:
            missing.append(q)
        elif not nq:
            # si no parece ISBN (URL), no lo filtramos por ISBN
            # (si querés soporte URL->URL, lo sumamos después)
            missing.append(q)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
    print(f"[OK] input={len(queries)} found={len(found)} missing={len(missing)} -> {out_path}")

if __name__ == "__main__":
    main()
