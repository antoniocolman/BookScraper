import argparse
import sqlite3

def get_cols(cur, db_prefix: str, table: str):
    rows = cur.execute(f"PRAGMA {db_prefix}.table_info({table})").fetchall()
    return [r[1] for r in rows]  # name

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dst", required=True, help="DB destino (booksearchv2.db)")
    ap.add_argument("--src", required=True, help="DB fuente (booksearchv2_new.db)")
    ap.add_argument("--table", default="book_std")
    args = ap.parse_args()

    con = sqlite3.connect(args.dst)
    cur = con.cursor()
    cur.execute(f"ATTACH DATABASE ? AS srcdb", (args.src,))

    dst_cols = get_cols(cur, "main", args.table)
    src_cols = get_cols(cur, "srcdb", args.table)

    common = [c for c in dst_cols if c in src_cols]
    if not common:
        raise SystemExit("No hay columnas comunes entre src y dst (¿mismo schema?)")

    col_list = ", ".join([f'"{c}"' for c in common])

    con.execute("BEGIN")
    cur.execute(f"""
        INSERT OR REPLACE INTO {args.table} ({col_list})
        SELECT {col_list}
        FROM srcdb.{args.table}
    """)
    changed = cur.rowcount
    con.commit()

    cur.execute("DETACH DATABASE srcdb")
    con.close()

    print(f"[OK] Merge terminado. Filas insert/replace: {changed}")

if __name__ == "__main__":
    main()