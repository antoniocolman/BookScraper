# app/sites/casa_del_libro.py
from __future__ import annotations

"""Casa del Libro - wrapper estable.

Objetivo
- Exponer un "site" estable para el CLI maestro (app/main.py).
- Mantener una API simple para uso directo:
    - search(q)   -> list[str] de URLs de productos
    - product(url)-> dict normalizado (raw_api OFF por defecto)

Este wrapper delega en: app.sites.experimental.casa_del_libro_experimental
"""

import re
import time
from typing import Any, Dict, List, Optional

SITE_ID = "casa_del_libro"
SITE_NAME = "Casa del Libro"

# app/main.py detecta capabilities por presencia de run_single / run_from_file
CAPABILITIES = ("query", "query-file")

HELP = """\
Casa del Libro (ES)

Funciones principales:
- search(q, rows=24) -> List[str] URLs de resultados
- product(url, include_raw_api=False) -> Dict normalizado

Este sitio usa un módulo experimental basado en:
- API Empathy (búsqueda y enriquecimiento)
- HTML de la ficha (breadcrumb, sinopsis, ficha técnica)
"""

_RE_WS = re.compile(r"\s+")


def _engine():
    """Import lazy del engine (search/detail) para no romper el discovery."""
    try:
        from .engines import search as cdl  # type: ignore
        return cdl
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "No pude importar app.sites.casa_del_libro.engines.search. "
            "Asegurate de que existe y que sus dependencias estén instaladas (httpx, selectolax). "
            f"Detalle: {e}"
        )

def _clean_str(v: Any) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\xa0", " ")
    s = _RE_WS.sub(" ", s).strip()
    return s

def _normalize_isbn(v: Any) -> str:
    s = _clean_str(v).upper()
    s = re.sub(r"[^0-9X]", "", s)
    return s

def _is_isbnish(v: Any) -> bool:
    s = _normalize_isbn(v)
    if len(s) not in (10, 13):
        return False
    if all(ch.isdigit() for ch in s):
        return True
    # permite ISBN-10 terminando en X
    return len(s) == 10 and s[:-1].isdigit() and s[-1] == "X"


# -----------------------------
# API pedida (wrapper simple)
# -----------------------------

def search(q: str, *, rows: int = 24) -> List[str]:
    """Devuelve URLs de productos para una consulta (ISBN o título)."""
    q = (q or "").strip()
    if not q:
        return []

    cdl = _engine()

    with cdl.make_client() as client:
        if cdl.is_isbn_query(q):
            items = cdl.api_isbnsearch(client, _normalize_isbn(q), start=0, rows=int(rows))
        else:
            items = cdl.api_search(client, q, start=0, rows=int(rows))

    out: List[str] = []
    seen = set()
    for it in items:
        u = _clean_str(it.get("url") or it.get("__url") or "")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= rows:
            break

    return out


def product(url: str, *, include_raw_api: bool = False) -> Dict[str, Any]:
    """Parsea un producto y normaliza claves.

    raw_api OFF por defecto (include_raw_api=False).
    """
    url = _clean_str(url)
    if not url:
        return {}

    cdl = _engine()
    data: Dict[str, Any] = dict(cdl.build_product(url) or {})

    raw_api = data.get("raw_api")
    if not include_raw_api:
        data.pop("raw_api", None)

    info = data.get("info_adicional")
    if not isinstance(info, dict):
        info = {}

    # Enriquecemos info_adicional con algunos campos útiles/consistentes
    if data.get("encuadernacion") and not info.get("encuadernacion"):
        info["encuadernacion"] = data.get("encuadernacion")
    if data.get("moneda") and not info.get("moneda"):
        info["moneda"] = data.get("moneda")
    if data.get("categoria_path") and not info.get("categoria_path"):
        info["categoria_path"] = data.get("categoria_path")

    for k in ("editorial", "idioma", "paginas", "dimensiones", "fecha_publicacion", "categoria"):
        if data.get(k) and not info.get(k):
            info[k] = data.get(k)

    row: Dict[str, Any] = {
        # claves estándar usadas por app/main.py para mapear a DB
        "url_detalle": _clean_str(data.get("url") or url),
        "titulo": _clean_str(data.get("titulo")),
        "autor": _clean_str(data.get("autor")),
        "isbn": _normalize_isbn(data.get("isbn")),
        "precio": data.get("precio"),
        "descripcion": _clean_str(data.get("descripcion")),
        "info_adicional": info,
        "portada_url": _clean_str(data.get("url_portada")),

        # extras (ayudan a QC/DB scoring)
        "editorial": _clean_str(data.get("editorial")),
        "idioma": _clean_str(data.get("idioma")),
        "paginas": _clean_str(data.get("paginas")),
        "dimensiones": _clean_str(data.get("dimensiones")),
        "peso": _clean_str(data.get("peso")),
        "fecha_publicacion": _clean_str(data.get("fecha_publicacion")),
        "categoria": _clean_str(data.get("categoria")),
        "moneda": _clean_str(data.get("moneda") or "EUR"),
        "encuadernacion": _clean_str(data.get("encuadernacion")),
    }

    if include_raw_api:
        row["raw_api"] = raw_api

    return row

# -----------------------------
# Hooks esperados por app/main.py
# -----------------------------

def run_single(
    query: str,
    *,
    max_results: int = 4,
    delay: float = 0.0,
    rows: int = 24,
    include_raw_api: bool = False,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Ejecuta una consulta (ISBN / título / URL) y devuelve filas estándar."""
    q = (query or "").strip()
    if not q:
        return []

    # si es URL, parseamos directo
    if q.lower().startswith("http"):
        return [product(q, include_raw_api=include_raw_api)]

    want_isbn = _is_isbnish(q)
    isbn_norm = _normalize_isbn(q) if want_isbn else ""

    # traemos más URLs para poder filtrar por ISBN cuando aplica
    seed_rows = max(int(rows), int(max_results) * 8)
    urls = search(q, rows=seed_rows)

    out: List[Dict[str, Any]] = []
    for i, u in enumerate(urls):
        if delay and i > 0:
            time.sleep(float(delay))

        try:
            row = product(u, include_raw_api=include_raw_api)
        except Exception:
            continue

        # filtro por ISBN si la query era ISBN
        if want_isbn:
            if _normalize_isbn(row.get("isbn")) != isbn_norm:
                continue

        # filtro anti-basura
        if not row.get("titulo"):
            continue

        out.append(row)
        if len(out) >= max_results:
            break

    return out

def run_from_file(
    query_file: str,
    *,
    limit: Optional[int] = None,
    limit_queries: Optional[int] = None,
    max_results_per_query: int = 1,
    max_results: Optional[int] = None,
    delay: float = 0.0,
    include_raw_api: bool = False,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Lee queries de un TXT (una por línea) y acumula resultados.

    Compatibilidad con el CLI maestro:
    - Puede pasar `max_results` cuando ejecuta en batch (y `max_results_per_query` también).
      Normalizamos para evitar: "got multiple values for keyword argument 'max_results'".
    - Puede pasar `limit_queries`; lo tratamos como alias de `limit`.
    """
    from pathlib import Path

    p = Path(query_file)
    if not p.exists():
        raise FileNotFoundError(f"No existe el archivo: {query_file}")

    # Alias tolerantes (compatibilidad con app/main.py)
    if limit is None:
        if limit_queries is not None:
            limit = int(limit_queries)
        elif "limit_queries" in kwargs:
            try:
                limit = int(kwargs.pop("limit_queries"))
            except Exception:
                kwargs.pop("limit_queries", None)

    # Resolver max_results de forma segura (evitar pasar dos veces)
    if max_results is None:
        mr = kwargs.pop("max_results", None)
        if mr is not None:
            try:
                max_results = int(mr)
            except Exception:
                max_results = None

    if max_results is None:
        max_results = int(max_results_per_query)

    # Evitar parámetros duplicados
    kwargs.pop("max_results_per_query", None)

    rows: List[Dict[str, Any]] = []
    n = 0
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        q = line.strip()
        if not q or q.startswith("#"):
            continue

        n += 1
        if limit and n > limit:
            break

        items = run_single(
            q,
            max_results=int(max_results),
            delay=delay,
            include_raw_api=include_raw_api,
            **kwargs,
        )
        rows.extend(items)

    return rows

__all__ = [
    "SITE_ID",
    "SITE_NAME",
    "CAPABILITIES",
    "HELP",
    "search",
    "product",
    "run_single",
    "run_from_file",
]