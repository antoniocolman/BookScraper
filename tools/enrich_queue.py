# tools/enrich_queue.py
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=r".\data\booksearchv2.db")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--output", default=r".\data\exports\enrich_queue_amazon.csv")
    args = ap.parse_args()

    con = sqlite3.connect(str(Path(args.db_path)))
    con.row_factory = sqlite3.Row

    q = """
    SELECT site, url, isbn, titulo,
           editorial, idioma, paginas, dimensiones, fecha_publicacion, url_portada,
           CASE WHEN sinopsis IS NULL THEN '' ELSE substr(sinopsis,1,30) END AS sinopsis_head
    FROM book_std
    WHERE trim(ifnull(isbn,'')) <> ''
      AND site <> 'amazon_books'
      AND (
        trim(ifnull(editorial,'')) = '' OR
        trim(ifnull(idioma,'')) = '' OR
        trim(ifnull(paginas,'')) = '' OR
        trim(ifnull(dimensiones,'')) = '' OR
        trim(ifnull(fecha_publicacion,'')) = '' OR
        trim(ifnull(url_portada,'')) = '' OR
        trim(ifnull(sinopsis,'')) = ''
      )
    ORDER BY site, isbn
    LIMIT ?
    """
    rows = con.execute(q, (args.limit,)).fetchall()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["isbn", "site", "url", "titulo", "amazon_search_url"])
        for r in rows:
            isbn = r["isbn"]
            amazon = f"https://www.amazon.com/s?k={isbn}&i=stripbooks"
            w.writerow([isbn, r["site"], r["url"], r["titulo"], amazon])

    con.close()
    print(f"[OK] Cola generada: {args.output} | items={len(rows)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
