from __future__ import annotations

import difflib
import re
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from app.config import EXPORTS_DIR

SITE_ID = "contrapunto"
SITE_NAME = "Contrapunto"
SITE_DESC = "Contrapunto (Shopify) - search + parse product (engine)"

# API esperada por registry/CLI
CAPABILITIES = ("query", "query-file")
DEFAULT_OUTPUT = str(EXPORTS_DIR / "contrapunto_resultados.csv")


def _engine():
    """Lazy import del engine para no romper 'python -m app sites'."""
    try:
        from .engines import search as eng  # type: ignore
        return eng
    except Exception as e:
        raise RuntimeError(
            "No pude importar app.sites.contrapunto.engines.search. "
            "Verificá que exista y que tengas instalados requests + beautifulsoup4.\n"
            f"Detalle: {e}"
        )

def _is_isbnish(s: str) -> bool:
    s = (s or "").strip()
    # permite guiones/espacios/X
    return bool(re.fullmatch(r"[0-9Xx\-\s]{10,20}", s))

def _normalize_isbn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", (s or "").strip()).upper()

def _score(query: str, item: Dict[str, Any]) -> float:
    q = (query or "").strip()
    if not q:
        return 0.0

    if _is_isbnish(q):
        qi = _normalize_isbn(q)
        ii = _normalize_isbn(item.get("isbn") or "")
        return 10.0 if ii and ii == qi else 0.0

    t = (item.get("titulo") or "").strip()
    if not t:
        return 0.0
    return difflib.SequenceMatcher(None, q.lower(), t.lower()).ratio()

def _to_raw_row(p: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte ContraPuntoBook(asdict) a raw estándar."""
    return {
        "url_detalle": p.get("url") or "",
        "titulo": p.get("titulo") or "",
        "autor": p.get("autor") or "",
        "isbn": p.get("isbn") or "",
        "precio": p.get("precio"),
        "descripcion": p.get("sinopsis") or "",
        "portada_url": p.get("url_portada") or "",
        "portada_local": "",
        "info_adicional": {
            "moneda": p.get("moneda") or "CLP",
            "disponibilidad": p.get("disponibilidad") or "",
            "editorial": p.get("editorial") or "",
            "encuadernacion": p.get("encuadernacion") or "",
            "idioma": p.get("idioma") or "",
            "paginas": p.get("paginas"),
            "dimensiones": p.get("dimensiones") or "",
            "fecha_publicacion": p.get("fecha_publicacion") or "",
            "precio_tachado": p.get("precio_tachado"),
            "badges": p.get("badges") or [],
            "tags": p.get("tags") or [],
        },
        "sitio": SITE_ID,
    }

def _search_urls_http(
    session: requests.Session,
    q: str,
    *,
    pages: int = 1,
    limit: int = 40,
    delay: float = 0.8,
    timeout: float = 30.0,
    retries: int = 3,
) -> List[str]:
    """Busca en Shopify /search y devuelve URLs de /products/..."""
    eng = _engine()
    urls: List[str] = []
    seen = set()

    pages = max(1, int(pages))
    for page in range(1, pages + 1):
        base = eng.build_search_url(q, prefix_last=True, base=eng.BASE_URL)
        url = f"{base}&{urlencode({'page': page})}"

        r = eng.http_get(session, url, timeout=timeout, retries=retries)
        page_urls = eng.extract_product_urls_from_listing(r.text or "")

        for u in page_urls:
            if not u or u in seen:
                continue
            seen.add(u)
            urls.append(u)
            if limit and len(urls) >= limit:
                break

        if limit and len(urls) >= limit:
            break

        if delay and page < pages:
            time.sleep(float(delay))

    return urls

def run_single(
    query: str,
    *,
    max_results: int = 3,
    delay: float = 0.8,
    timeout: float = 60.0,
    pages: int = 1,
    limit: int = 40,
    retries: int = 3,
    prefer_js: bool = True,
    session: Optional[requests.Session] = None,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    eng = _engine()
    sess = session or eng.get_session()

    # URL directa
    if q.lower().startswith("http"):
        book = eng.parse_product(sess, q, prefer_js=prefer_js, timeout=float(timeout), retries=int(retries), sleep=0.0)
        return [_to_raw_row(asdict(book))] if book else []

    urls = _search_urls_http(
        sess,
        q,
        pages=pages,
        limit=max(limit, max_results * 12),
        delay=delay,
        timeout=min(30.0, float(timeout)),
        retries=retries,
    )
    if not urls:
        return []

    parsed: List[Dict[str, Any]] = []

    for idx, u in enumerate(urls, start=1):
        if delay and idx > 1:
            time.sleep(float(delay))

        try:
            book = eng.parse_product(sess, u, prefer_js=prefer_js, timeout=float(timeout), retries=int(retries), sleep=0.0)
        except Exception:
            continue

        d = asdict(book)

        # Si es ISBN, filtrar por match exacto
        if _is_isbnish(q):
            if _normalize_isbn(d.get("isbn") or "") != _normalize_isbn(q):
                continue

        parsed.append(d)

        if len(parsed) >= max_results * 3 and _is_isbnish(q):
            break

    parsed.sort(key=lambda d: _score(q, d), reverse=True)
    return [_to_raw_row(p) for p in parsed[:max_results]]

def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    max_results: int = 3,
    delay: float = 0.8,
    query_delay: float = 0.0,
    batch_size: int = 0,
    batch_pause: float = 0.0,
    timeout: float = 60.0,
    retries: int = 3,
    prefer_js: bool = True,
    session: Optional[requests.Session] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    eng = _engine()
    sess = session or eng.get_session()

    def _iter_queries(path: str):
        n = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = (line or "").strip()
                if not raw or raw.startswith("#"):
                    continue
                parts = re.split(r"\t|\|", raw, maxsplit=1)
                q = parts[0].strip() if parts else raw
                n += 1
                if limit_queries and n > limit_queries:
                    break
                yield n, q

    batch_count = 0
    for idx, q in _iter_queries(query_file):
        if query_delay and idx > 1:
            time.sleep(float(query_delay))

        rows = run_single(
            q,
            max_results=max_results,
            delay=delay,
            timeout=timeout,
            retries=retries,
            prefer_js=prefer_js,
            session=sess,
            **kwargs,
        )
        out.extend(rows)

        if batch_size and batch_pause:
            batch_count += 1
            if batch_count >= batch_size:
                time.sleep(float(batch_pause))
                batch_count = 0

    return out

__all__ = [
    "SITE_ID",
    "SITE_NAME",
    "SITE_DESC",
    "CAPABILITIES",
    "DEFAULT_OUTPUT",
    "run_single",
    "run_from_file",
]