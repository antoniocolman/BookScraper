import argparse
import csv
import sqlite3
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/booksearchv2.db", help="Ruta a la DB SQLite")
    ap.add_argument("--out", default="data/exports/book_master.csv", help="CSV de salida")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db)
    try:
        cur = con.execute("select * from book_master")
        cols = [d[0] for d in cur.description]

        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur.fetchall())

        print(f"[OK] Export -> {out} ({out.stat().st_size} bytes)")
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
