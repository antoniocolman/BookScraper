from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse, urlencode

import requests

SITE_ID = "contrapunto"
SITE_NAME = "Contrapunto"
SITE_DESC = "Contrapunto (Shopify) - search HTML + parse product via experimental"

BASE_URL = "https://contrapunto.cl"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
)

# Script experimental (solo usamos cmd=product)
EXP_SCRIPT = Path(__file__).resolve().parent / "experimental" / "contrapunto_experimental.py"


def _canonicalize_product_url(url: str) -> str:
    """
    Limpia URL quitando query/fragment, y asegura base_url.
    """
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = urljoin(BASE_URL, url)

    p = urlparse(url)
    clean = p._replace(query="", fragment="")
    return urlunparse(clean)


def _is_isbnish(s: str) -> bool:
    s = (s or "").strip()
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


def _run_exp_product(url: str, *, timeout: float = 60.0) -> Dict[str, Any]:
    """
    Ejecuta: python contrapunto_experimental.py product --url <url>
    Devuelve dict parseado (JSON).
    """
    if not EXP_SCRIPT.exists():
        raise RuntimeError(f"No existe EXP_SCRIPT: {EXP_SCRIPT}")

    cmd = [sys.executable, str(EXP_SCRIPT), "product", "--url", url]
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="ignore",
    )
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()

    if p.returncode != 0 and not out:
        raise RuntimeError(err or f"contrapunto_experimental falló (rc={p.returncode})")

    # recortar JSON del stdout (por si imprime algo adicional)
    i = out.find("{")
    j = out.rfind("}")
    if i >= 0 and j > i:
        return json.loads(out[i : j + 1])
    return {}


def _search_urls_http(
    session: requests.Session,
    q: str,
    *,
    pages: int = 1,
    limit: int = 40,
    delay: float = 0.8,
    timeout: float = 30.0,
) -> List[str]:
    """
    Busca en /search y devuelve URLs de /products/...
    """
    urls: List[str] = []
    seen = set()

    headers = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}

    for page in range(1, max(1, int(pages)) + 1):
        params = {
            "options[prefix]": "last",
            "q": q,
            "page": page,
        }
        url = f"{BASE_URL}/search?{urlencode(params)}"
        r = session.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        html = r.text or ""

        # Regex simple para agarrar hrefs a productos
        for m in re.finditer(r'href="([^"]*?/products/[^"]+)"', html, flags=re.IGNORECASE):
            href = m.group(1)
            if "/products/" not in href:
                continue
            full = _canonicalize_product_url(href)
            if not full or full in seen:
                continue
            seen.add(full)
            urls.append(full)
            if limit and len(urls) >= limit:
                break

        if limit and len(urls) >= limit:
            break

        if delay and page < pages:
            time.sleep(float(delay))

    return urls


def _to_raw_row(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convierte JSON del experimental a raw estándar para standard.py
    """
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
            "precio": p.get("precio"),
        },
        "sitio": SITE_ID,
    }

def run_single(
    query: str,
    *,
    max_results: int = 3,
    delay: float = 0.8,
    timeout: float = 60.0,
    pages: int = 1,
    limit: int = 40,
    session: Optional[requests.Session] = None,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    sess = session or requests.Session()

    # URL directa
    if q.lower().startswith("http"):
        p = _run_exp_product(_canonicalize_product_url(q), timeout=timeout)
        return [_to_raw_row(p)] if p else []

    # Buscar URLs en la web
    urls = _search_urls_http(
        sess,
        q,
        pages=pages,
        limit=max(limit, max_results * 12),
        delay=delay,
        timeout=min(30.0, float(timeout)),
    )
    if not urls:
        return []

    parsed: List[Dict[str, Any]] = []

    for idx, u in enumerate(urls, start=1):
        if delay and idx > 1:
            time.sleep(float(delay))

        try:
            p = _run_exp_product(u, timeout=timeout)
        except Exception:
            continue

        if not p:
            continue

        # Si es ISBN, filtrar por match exacto
        if _is_isbnish(q):
            if _normalize_isbn(p.get("isbn") or "") != _normalize_isbn(q):
                continue

        parsed.append(p)

        if len(parsed) >= max_results * 3 and _is_isbnish(q):
            # con ISBN con 1 match suele bastar
            break

    # rank
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
    session: Optional[requests.Session] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    sess = session or requests.Session()

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