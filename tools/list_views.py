import sqlite3

con = sqlite3.connect('data/booksearchv2.db')
try:
    rows = list(con.execute("select name, sql from sqlite_master where type='view' order by name"))
    print("VIEWS:", len(rows))
    for name, sql in rows:
        print("\n==", name, "==")
        print(sql)
finally:
    con.close()
