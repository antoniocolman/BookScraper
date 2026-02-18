from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from sqlalchemy import bindparam, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.sql import Insert

from app.db.models import book_std


def _insert_for_dialect(conn: Connection) -> Insert:
    name = conn.engine.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert(book_std)
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        return sqlite_insert(book_std)
    raise RuntimeError(f"Unsupported dialect for upsert: {name}")


def upsert_many(conn: Connection, rows: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    if not rows:
        return (0, 0)

    ins = _insert_for_dialect(conn)
    update_cols = {c.name: getattr(ins.excluded, c.name) for c in book_std.c if c.name not in ("site", "url")}

    stmt = ins.on_conflict_do_update(
        index_elements=[book_std.c.site, book_std.c.url],
        set_=update_cols,
    )

    result = conn.execute(stmt, list(rows))
    inserted = result.rowcount or 0
    # SQLAlchemy does not separate insert/update counts for upsert.
    return (inserted, 0)


def fetch_all(conn: Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(select(book_std)).mappings().all()
    return [dict(r) for r in rows]


def fetch_by_isbns(conn: Connection, isbns: Sequence[str], chunk: int = 900) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isbns:
        return out

    for i in range(0, len(isbns), chunk):
        part = list(isbns[i:i + chunk])
        stmt = select(book_std).where(book_std.c.isbn.in_(bindparam("isbns", expanding=True)))
        rows = conn.execute(stmt, {"isbns": part}).mappings().all()
        out.extend([dict(r) for r in rows])

    return out


def isbns_present(conn: Connection, isbns: Sequence[str], chunk: int = 900) -> List[str]:
    found: List[str] = []
    if not isbns:
        return found

    for i in range(0, len(isbns), chunk):
        part = list(isbns[i:i + chunk])
        stmt = select(book_std.c.isbn).where(book_std.c.isbn.in_(bindparam("isbns", expanding=True)))
        rows = conn.execute(stmt, {"isbns": part}).fetchall()
        found.extend([str(r[0]) for r in rows if r and r[0]])

    seen = set()
    out = []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def fetch_for_qc(conn: Connection) -> List[Dict[str, Any]]:
    stmt = select(
        book_std.c.site,
        book_std.c.isbn,
        book_std.c.titulo,
        book_std.c.url,
        book_std.c.raw_json,
    ).where(book_std.c.raw_json.is_not(None))
    rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def fetch_missing_fields(conn: Connection, limit: int = 300) -> List[Dict[str, Any]]:
    missing = or_(
        book_std.c.editorial.is_(None),
        book_std.c.editorial == "",
        book_std.c.idioma.is_(None),
        book_std.c.idioma == "",
        book_std.c.paginas.is_(None),
        book_std.c.paginas == "",
        book_std.c.dimensiones.is_(None),
        book_std.c.dimensiones == "",
        book_std.c.fecha_publicacion.is_(None),
        book_std.c.fecha_publicacion == "",
        book_std.c.url_portada.is_(None),
        book_std.c.url_portada == "",
        book_std.c.sinopsis.is_(None),
        book_std.c.sinopsis == "",
    )
    stmt = (
        select(
            book_std.c.site,
            book_std.c.url,
            book_std.c.isbn,
            book_std.c.titulo,
            book_std.c.editorial,
            book_std.c.idioma,
            book_std.c.paginas,
            book_std.c.dimensiones,
            book_std.c.fecha_publicacion,
            book_std.c.url_portada,
            book_std.c.sinopsis,
        )
        .where(book_std.c.isbn.is_not(None))
        .where(book_std.c.isbn != "")
        .where(book_std.c.site != "amazon_books")
        .where(missing)
        .limit(limit)
    )
    rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]
