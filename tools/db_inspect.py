import sqlite3
from pathlib import Path

DB = Path("data") / "booksearchv2.db"

def main():
    con = sqlite3.connect(str(DB))
    try:
        print("DB:", DB.resolve())

        print("\n[PRAGMA] index_list(book_std):")
        rows = list(con.execute("PRAGMA index_list(book_std)"))
        for r in rows:
            print(" ", r)

        # Buscar el autoindex (pk/unique) automáticamente
        auto = None
        for r in rows:
            name = r[1]
            if name.startswith("sqlite_autoindex_book_std_"):
                auto = name
                break

        if not auto:
            print("\n[WARN] No encontré sqlite_autoindex_book_std_*. Puede ser que no haya PK/UNIQUE.")
        else:
            print(f"\n[PRAGMA] index_info({auto}):")
            info = list(con.execute(f"PRAGMA index_info('{auto}')"))
            for r in info:
                print(" ", r)

            cols = [r[2] for r in info]
            print("\n[PK/UNIQUE columns]:", cols)

        print("\n[SQL] CREATE TABLE book_std:")
        sql = con.execute(
            "select sql from sqlite_master where type='table' and name='book_std'"
        ).fetchone()
        print(sql[0] if sql else "(no encontrado)")

    finally:
        con.close()

if __name__ == "__main__":
    main()
