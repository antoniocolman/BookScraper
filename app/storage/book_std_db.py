from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.standard import normalize_encuadernacion

# IMPORTANTE: mantener esto alineado con app/standard.py
STD_FIELDS = [
    "ISBN",
    "TITULO",
    "AUTOR",
    "EDITORIAL",
    "ENCUADERNACION",
    "CATEGORIA",
    "SINOPSIS",
    "IDIOMA",
    "PAGINAS",
    "DIMENSIONES",
    "FECHA PUBLICACION",
    "URL",
    "URL PORTADA",
    "SITE",
]


def normalize_isbn(isbn: Optional[str]) -> str:
    if not isbn:
        return ""
    s = re.sub(r"[^0-9Xx]", "", str(isbn)).strip()
    return s.upper()


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _ensure_columns(con: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    cur = con.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cur.fetchall()}
    for col, coltype in columns.items():
        if col not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    con.commit()


def init_db(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS book_std (
            site TEXT NOT NULL,
            url TEXT NOT NULL,
            isbn TEXT,
            titulo TEXT,
            autor TEXT,
            editorial TEXT,
            encuadernacion TEXT,
            categoria TEXT,
            sinopsis TEXT,
            idioma TEXT,
            paginas TEXT,
            dimensiones TEXT,
            fecha_publicacion TEXT,
            url_portada TEXT,
            raw_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (site, url)
        )
        """
    )
    con.commit()

    # Migraciones suaves (para DBs existentes)
    _ensure_columns(
        con,
        "book_std",
        {
            "encuadernacion": "TEXT",
            "categoria": "TEXT",
            "raw_json": "TEXT",
        },
    )

    con.execute("CREATE INDEX IF NOT EXISTS idx_book_std_isbn ON book_std(isbn)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_book_std_site ON book_std(site)")
    con.commit()


def _derive_encuadernacion_from_info(info: Dict[str, Any]) -> str:
    def _clean(x: Any) -> str:
        return str(x).strip() if x is not None else ""

    enc = _clean(info.get("encuadernacion"))
    binding = _clean(info.get("binding"))
    fmt = _clean(info.get("formato") or info.get("format"))
    tapa = _clean(info.get("tapa") or info.get("cover"))

    cand = enc or binding or fmt or tapa
    return normalize_encuadernacion(cand)


def _coerce_to_std_row(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convierte cualquier row (raw o std) a dict estándar (STD_FIELDS).
    """
    # Caso 1: ya viene como std
    if "ISBN" in obj and "URL" in obj and "SITE" in obj:
        return {
            "ISBN": normalize_isbn(obj.get("ISBN")),
            "TITULO": obj.get("TITULO") or "",
            "AUTOR": obj.get("AUTOR") or "",
            "EDITORIAL": obj.get("EDITORIAL") or "",
            "ENCUADERNACION": normalize_encuadernacion(obj.get("ENCUADERNACION") or ""),
            "CATEGORIA": obj.get("CATEGORIA") or "",
            "SINOPSIS": obj.get("SINOPSIS") or "",
            "IDIOMA": obj.get("IDIOMA") or "",
            "PAGINAS": obj.get("PAGINAS") or "",
            "DIMENSIONES": obj.get("DIMENSIONES") or "",
            "FECHA PUBLICACION": obj.get("FECHA PUBLICACION") or "",
            "URL": obj.get("URL") or "",
            "URL PORTADA": obj.get("URL PORTADA") or "",
            "SITE": obj.get("SITE") or "",
        }

    # Caso 2: legacy/raw (si viene de algún scraper “viejo”)
    raw_json = obj.get("raw_json")
    info = obj.get("info_adicional") or {}
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except Exception:
            info = {}

    if (not info) and raw_json:
        try:
            d = json.loads(raw_json)
            if isinstance(d, dict):
                info = d.get("info_adicional") or info
        except Exception:
            pass

    encuadernacion = _derive_encuadernacion_from_info(info if isinstance(info, dict) else {})

    categoria = ""
    if isinstance(info, dict):
        categoria = (info.get("categoria") or info.get("categoría") or info.get("category") or "") or ""

    return {
        "ISBN": normalize_isbn(obj.get("isbn") or ""),
        "TITULO": obj.get("titulo") or "",
        "AUTOR": obj.get("autor") or "",
        "EDITORIAL": (info.get("editorial") if isinstance(info, dict) else "") or "",
        "ENCUADERNACION": encuadernacion,
        "CATEGORIA": categoria,
        "SINOPSIS": obj.get("descripcion") or "",
        "IDIOMA": (info.get("idioma") if isinstance(info, dict) else "") or "",
        "PAGINAS": (info.get("paginas") if isinstance(info, dict) else "") or "",
        "DIMENSIONES": (info.get("dimensiones") if isinstance(info, dict) else "") or "",
        "FECHA PUBLICACION": (info.get("fecha_publicacion") if isinstance(info, dict) else "") or "",
        "URL": obj.get("url_detalle") or "",
        "URL PORTADA": obj.get("portada_url") or "",
        "SITE": obj.get("sitio") or "",
    }


def upsert_many(con: sqlite3.Connection, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Inserta/actualiza filas std.
    IMPORTANTE: guarda raw_json como:
      - r["_raw_json"] si existe (wrapper raw/std)
      - sino json.dumps(r) (std plano) como fallback
    """
    if not rows:
        return (0, 0)

    inserted = 0
    updated = 0

    sql = """
    INSERT INTO book_std (
        site, url, isbn, titulo, autor, editorial, encuadernacion, categoria,
        sinopsis, idioma, paginas, dimensiones, fecha_publicacion, url_portada, raw_json, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    ON CONFLICT(site, url) DO UPDATE SET
        isbn=excluded.isbn,
        titulo=excluded.titulo,
        autor=excluded.autor,
        editorial=excluded.editorial,
        encuadernacion=excluded.encuadernacion,
        categoria=excluded.categoria,
        sinopsis=excluded.sinopsis,
        idioma=excluded.idioma,
        paginas=excluded.paginas,
        dimensiones=excluded.dimensiones,
        fecha_publicacion=excluded.fecha_publicacion,
        url_portada=excluded.url_portada,
        raw_json=excluded.raw_json,
        updated_at=datetime('now')
    """

    for r in rows:
        site = r.get("SITE") or ""
        url = r.get("URL") or ""
        if not site or not url:
            continue

        # detectar si existía antes (para conteo insert/update)
        exists = con.execute(
            "SELECT 1 FROM book_std WHERE site=? AND url=? LIMIT 1",
            (site, url),
        ).fetchone() is not None

        raw_json = r.get("_raw_json")
        if not raw_json:
            # fallback: guardar std plano
            raw_json = json.dumps(r, ensure_ascii=False, default=str)

        payload = (
            site,
            url,
            normalize_isbn(r.get("ISBN")),
            r.get("TITULO") or "",
            r.get("AUTOR") or "",
            r.get("EDITORIAL") or "",
            normalize_encuadernacion(r.get("ENCUADERNACION") or ""),
            r.get("CATEGORIA") or "",
            r.get("SINOPSIS") or "",
            r.get("IDIOMA") or "",
            r.get("PAGINAS") or "",
            r.get("DIMENSIONES") or "",
            r.get("FECHA PUBLICACION") or "",
            r.get("URL PORTADA") or "",
            raw_json,
        )

        con.execute(sql, payload)
        if exists:
            updated += 1
        else:
            inserted += 1

    con.commit()
    return (inserted, updated)


def fetch_all(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    q = """
    SELECT
        isbn AS "ISBN",
        titulo AS "TITULO",
        autor AS "AUTOR",
        editorial AS "EDITORIAL",
        encuadernacion AS "ENCUADERNACION",
        categoria AS "CATEGORIA",
        sinopsis AS "SINOPSIS",
        idioma AS "IDIOMA",
        paginas AS "PAGINAS",
        dimensiones AS "DIMENSIONES",
        fecha_publicacion AS "FECHA PUBLICACION",
        url AS "URL",
        url_portada AS "URL PORTADA",
        site AS "SITE"
    FROM book_std
    ORDER BY site, isbn, titulo
    """
    rows = con.execute(q).fetchall()
    return [dict(r) for r in rows]


def fetch_by_isbns(con: sqlite3.Connection, isbns: List[str]) -> List[Dict[str, Any]]:
    isbns_n = [normalize_isbn(x) for x in isbns if normalize_isbn(x)]
    if not isbns_n:
        return []

    out: List[Dict[str, Any]] = []
    CHUNK = 900

    for i in range(0, len(isbns_n), CHUNK):
        chunk = isbns_n[i : i + CHUNK]
        placeholders = ",".join(["?"] * len(chunk))
        q = f"""
        SELECT
            isbn AS "ISBN",
            titulo AS "TITULO",
            autor AS "AUTOR",
            editorial AS "EDITORIAL",
            encuadernacion AS "ENCUADERNACION",
            categoria AS "CATEGORIA",
            sinopsis AS "SINOPSIS",
            idioma AS "IDIOMA",
            paginas AS "PAGINAS",
            dimensiones AS "DIMENSIONES",
            fecha_publicacion AS "FECHA PUBLICACION",
            url AS "URL",
            url_portada AS "URL PORTADA",
            site AS "SITE"
        FROM book_std
        WHERE isbn IN ({placeholders})
        ORDER BY site, isbn, titulo
        """
        rows = con.execute(q, chunk).fetchall()
        out.extend([dict(r) for r in rows])

    return out


def isbns_present(con: sqlite3.Connection, isbns: List[str]) -> List[str]:
    isbns_n = [normalize_isbn(x) for x in isbns if normalize_isbn(x)]
    if not isbns_n:
        return []

    found: List[str] = []
    CHUNK = 900

    for i in range(0, len(isbns_n), CHUNK):
        chunk = isbns_n[i : i + CHUNK]
        placeholders = ",".join(["?"] * len(chunk))
        q = f"""
        SELECT DISTINCT isbn
        FROM book_std
        WHERE isbn IN ({placeholders}) AND isbn IS NOT NULL AND TRIM(isbn) <> ''
        """
        rows = con.execute(q, chunk).fetchall()
        found.extend([str(r[0]) for r in rows if r[0]])

    seen = set()
    out = []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class Database:
    """
    Adapter para el resto del proyecto.
    Clave: acá construimos _raw_json = {"raw":..., "std":...} antes del upsert.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path))
        self.con = connect(self.db_path)
        init_db(self.con)

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass

    def isbns_present(self, isbns: List[str]) -> List[str]:
        return isbns_present(self.con, isbns)

    def fetch_by_isbns(self, isbns: List[str]) -> List[Dict[str, Any]]:
        return fetch_by_isbns(self.con, isbns)

    def fetch_all(self) -> List[Dict[str, Any]]:
        return fetch_all(self.con)

    def upsert_books(self, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
        """
        Recibe filas raw o std.
        Para cada fila:
          - construye std
          - guarda wrapper en _raw_json
          - upsert a DB
        """
        std_rows: List[Dict[str, Any]] = []

        for raw_row in rows:
            std_row = _coerce_to_std_row(raw_row)

            wrapper = {"raw": raw_row, "std": std_row}
            std_row["_raw_json"] = json.dumps(wrapper, ensure_ascii=False, default=str)

            std_rows.append(std_row)

        return upsert_many(self.con, std_rows)

    # aliases por compat
    def upsert_batch(self, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
        return self.upsert_books(rows)

    def upsert_many(self, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
        return self.upsert_books(rows)
