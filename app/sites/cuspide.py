# app/sites/cuspide.py
from __future__ import annotations

"""Cúspide - wrapper estable para el CLI maestro.

Objetivo: devolver un dict "limpio" (sin campos de debug/duplicados), pero compatible
con el mapper a DB/CSV del main.

Salida (keys fijas):
  site, url_detalle, titulo, autor, editorial, isbn, idioma, paginas, dimensiones, peso,
  fecha_publicacion, fecha_primera_publicacion, categoria, descripcion,
  portada_url, precio, moneda
"""

from typing import Any, Dict, List, Optional
import re
import time

SITE_ID = "cuspide"
SITE_NAME = "Cúspide"
CAPABILITIES = ("query", "query-file")


def _is_isbnish(s: str) -> bool:
    s = re.sub(r"[^0-9Xx]", "", (s or "").strip())
    return len(s) in (10, 13)


def _normalize_isbn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", (s or "").strip()).upper()


def _exp():
    """Import lazy del experimental para no romper el discovery."""
    try:
        from app.sites.experimental import cuspide_experimental as cs  # type: ignore
        return cs
    except Exception as e:
        raise RuntimeError(
            "No pude importar app.sites.experimental.cuspide_experimental. "
            "Verificá que exista y que tengas instalados requests + beautifulsoup4.\n"
            f"Detalle: {e}"
        )


def _clean_str(v: Any) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _best_portada_url(url_portada: str, extra: Any, isbn: str) -> str:
    """Elige la portada más útil:
    - Preferir .jpg/.jpeg/.png que contenga el ISBN (si existe)
    - Si no, preferir jpg/jpeg/png sobre webp
    - Si no hay extra, usar url_portada
    """
    up = _clean_str(url_portada)
    isb = _normalize_isbn(isbn)

    urls: List[str] = []
    if isinstance(extra, list):
        for u in extra:
            uu = _clean_str(u)
            if uu:
                urls.append(uu)

    # incluir url_portada al set
    if up and up not in urls:
        urls.insert(0, up)

    if not urls:
        return ""

    def score(u: str) -> int:
        ul = u.lower()
        sc = 0
        if isb and isb in re.sub(r"[^0-9X]", "", ul.upper()):
            sc += 20000
        if ul.endswith(('.jpg', '.jpeg', '.png')):
            sc += 5000
        elif ul.endswith('.webp'):
            sc += 1000
        # penalizar "mediamodifier" (a veces es thumbnail/derivado)
        if "mediamodifier" in ul:
            sc -= 50
        return sc

    urls_sorted = sorted(urls, key=score, reverse=True)
    return urls_sorted[0]


def _book_to_row(book_obj: Any) -> Dict[str, Any]:
    """Convierte CuspideBook -> row dict limpio."""
    if book_obj is None:
        return {}

    # atributos esperados del dataclass del experimental
    url_detalle = _clean_str(getattr(book_obj, "url", "") or getattr(book_obj, "url_detalle", ""))
    titulo = _clean_str(getattr(book_obj, "titulo", ""))
    autor = _clean_str(getattr(book_obj, "autor", ""))
    editorial = _clean_str(getattr(book_obj, "editorial", ""))
    isbn = _normalize_isbn(getattr(book_obj, "isbn", "") or "")

    idioma = _clean_str(getattr(book_obj, "idioma", ""))
    paginas = _clean_str(getattr(book_obj, "paginas", ""))
    dimensiones = _clean_str(getattr(book_obj, "dimensiones", ""))
    peso = _clean_str(getattr(book_obj, "peso", ""))

    fecha_publicacion = _clean_str(getattr(book_obj, "fecha_publicacion", ""))
    fecha_primera_publicacion = _clean_str(getattr(book_obj, "fecha_primera_publicacion", ""))

    categoria = _clean_str(getattr(book_obj, "categoria", ""))
    descripcion = _clean_str(getattr(book_obj, "descripcion", ""))

    precio = getattr(book_obj, "precio", None)
    moneda = _clean_str(getattr(book_obj, "moneda", ""))

    url_portada = _clean_str(getattr(book_obj, "url_portada", "") or getattr(book_obj, "portada_url", ""))
    extra_imagenes = getattr(book_obj, "extra_imagenes", None) or getattr(book_obj, "all_images", None)
    portada_url = _best_portada_url(url_portada, extra_imagenes, isbn)

    # salida con keys fijas (limpia para CSV y para DB)
    return {
        "site": "cuspide",
        "url_detalle": url_detalle,
        "titulo": titulo,
        "autor": autor,
        "editorial": editorial,
        "isbn": isbn,
        "idioma": idioma,
        "paginas": paginas,
        "dimensiones": dimensiones,
        "peso": peso,
        "fecha_publicacion": fecha_publicacion,
        "fecha_primera_publicacion": fecha_primera_publicacion,
        "categoria": categoria,
        "descripcion": descripcion,
        "portada_url": portada_url,
        "precio": precio,
        "moneda": moneda,
    }


def run_single(
    query: str,
    *,
    max_results: int = 3,
    delay: float = 0.8,
    pages: int = 1,
    timeout: float = 30.0,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    cs = _exp()
    session = cs.get_session()

    # URL directa
    if q.lower().startswith("http"):
        b = cs.fetch_and_parse_product(session, q, timeout=timeout)
        row = _book_to_row(b)
        return [row] if row else []

    want_isbn = _is_isbnish(q)
    q_isbn = _normalize_isbn(q) if want_isbn else ""

    out: List[Dict[str, Any]] = []
    seen_urls = set()

    for url in cs.iter_search_results(
        session,
        q,
        limit_pages=max(1, int(pages)),
        delay=float(delay),
        timeout=float(timeout),
    ):
        if url in seen_urls:
            continue
        seen_urls.add(url)

        b = cs.fetch_and_parse_product(session, url, timeout=timeout)

        if want_isbn:
            b_isbn = _normalize_isbn(getattr(b, "isbn", "") or "")
            if not b_isbn or b_isbn != q_isbn:
                continue

        row = _book_to_row(b)
        if row:
            out.append(row)

        if len(out) >= max_results:
            break

        if delay:
            time.sleep(float(delay))

    return out


def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    max_results: int = 3,
    delay: float = 0.8,
    query_delay: float = 0.0,
    batch_size: int = 0,
    batch_pause: float = 0.0,
    pages: int = 1,
    timeout: float = 30.0,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    n = 0
    batch = 0

    with open(query_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = (line or "").strip()
            if not raw or raw.startswith("#"):
                continue

            n += 1
            if limit_queries and n > limit_queries:
                break

            if query_delay and n > 1:
                time.sleep(float(query_delay))

            rows = run_single(
                raw,
                max_results=max_results,
                delay=delay,
                pages=pages,
                timeout=timeout,
                **kwargs,
            )
            out.extend(rows)

            batch += 1
            if batch_size and batch_pause and batch >= batch_size:
                time.sleep(float(batch_pause))
                batch = 0

    return out