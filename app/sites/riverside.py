from __future__ import annotations

import difflib
import re
import time
from html import unescape
from math import ceil
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

import httpx

SITE_ID = "riverside"
SITE_NAME = "Riverside Agency"
CAPABILITIES = ["query", "query-file", "url"]

BASE = "https://www.riversideagency.com.ar"
SEARCH_PATH = "/busqueda-rapida.php"
DETAIL_PATH = "/libro.php"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_RE_ISBN = re.compile(r"[^0-9Xx]")

# Result cards
_RE_LI = re.compile(r"<li\b[^>]*>.*?</li>", re.I | re.S)
_RE_HREF = re.compile(r'href=["\'](libro\.php\?[^"\']*\bean=[^"\']+)["\']', re.I)
_RE_CARD_TITLE = re.compile(r'class=["\']titulo["\'][^>]*>(.*?)</a>', re.I | re.S)
_RE_CARD_AUTOR = re.compile(r'id=["\']autor["\'][^>]*>(.*?)</span>', re.I | re.S)
_RE_CARD_PRECIO = re.compile(r'id=["\']precio["\'][^>]*>(.*?)</span>', re.I | re.S)

# Pagination + totals
_RE_PAGINA = re.compile(r"[?&]pagina=(\d+)", re.I)
_RE_TOTALES = re.compile(r'(<div class="totales">.*?</div>)', re.I | re.S)
_RE_NUM_FOUND = re.compile(r"(\d+)\s*libros?\s*encontrados", re.I)

# Detail page
_RE_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_RE_LI_AUTOR = re.compile(r"<li>\s*Autor:\s*(.*?)</li>", re.I | re.S)
_RE_LI_EDITOR = re.compile(r"<li>\s*Editor:\s*(.*?)</li>", re.I | re.S)

_RE_DESC_BLOCK = re.compile(r"</header>(.*?)(?:<aside\s+class=[\"']autor|<dl>|</div>)", re.I | re.S)
_RE_P = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)

_RE_DD = lambda cls: re.compile(
    rf'<dd[^>]*class=["\'][^"\']*\b{re.escape(cls)}\b[^"\']*["\'][^>]*>(.*?)</dd>',
    re.I | re.S,
)

_RE_COVER_HREF = re.compile(r'id=["\']portada_imagen["\'][^>]*href=["\']([^"\']+)["\']', re.I)
_RE_COVER_IMG = re.compile(r"<figure>.*?<img[^>]*src=['\"]([^'\"]+)['\"]", re.I | re.S)

_RE_ASIDE_AUTOR = re.compile(r'<aside\s+class=["\']autor[^"\']*["\'].*?</aside>', re.I | re.S)
_RE_ASIDE_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.I | re.S)
_RE_ASIDE_P = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
_RE_ASIDE_IMG = re.compile(r"<img[^>]*src=['\"]([^'\"]+)['\"]", re.I | re.S)


def _strip_html(s: str) -> str:
    s = s or ""
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*p\s*>", "\n\n", s)
    s = _TAGS.sub(" ", s)
    s = unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = _WS.sub(" ", s).strip()
    return s


def _normalize_isbn(s: str) -> str:
    return _RE_ISBN.sub("", (s or "")).upper()


def _is_isbnish(s: str) -> bool:
    x = _normalize_isbn(s)
    return len(x) in (10, 13) and any(ch.isdigit() for ch in x)


def _parse_price_ars(raw: str) -> Optional[int]:
    if not raw:
        return None
    x = raw.replace("$", "").strip()
    # Riverside suele ser 38000.00
    if re.search(r"\.\d{2}$", x):
        x = x.rsplit(".", 1)[0]
    x = x.replace(".", "").replace(",", "")
    x = re.sub(r"[^0-9]", "", x)
    try:
        return int(x) if x else None
    except ValueError:
        return None


def _fetch_get(client: httpx.Client, url: str, timeout: float) -> str:
    r = client.get(url, follow_redirects=True, timeout=timeout)
    r.raise_for_status()
    return r.text


def _fetch_post_search(client: httpx.Client, query: str, timeout: float) -> str:
    r = client.post(urljoin(BASE, SEARCH_PATH), data={"busqueda": query}, follow_redirects=True, timeout=timeout)
    r.raise_for_status()
    return r.text


def _search_page(client: httpx.Client, query: str, page: int, timeout: float) -> str:
    # GET paginado
    url = f"{BASE}{SEARCH_PATH}?busqueda={quote_plus(query)}&pagina={page}"
    html = _fetch_get(client, url, timeout)

    # fallback: si en page=1 no aparecen cards, probar POST
    if page == 1 and not _RE_HREF.search(html):
        html2 = _fetch_post_search(client, query, timeout)
        if _RE_HREF.search(html2) or "No se encontraron libros" in html2:
            return html2
    return html


def _parse_search_items(html: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for li in _RE_LI.findall(html or ""):
        mh = _RE_HREF.search(li)
        if not mh:
            continue

        rel = mh.group(1).strip()
        url = urljoin(BASE + "/", rel)

        title = ""
        mt = _RE_CARD_TITLE.search(li)
        if mt:
            title = _strip_html(mt.group(1))

        author = ""
        ma = _RE_CARD_AUTOR.search(li)
        if ma:
            author = _strip_html(ma.group(1))

        price_raw = ""
        mp = _RE_CARD_PRECIO.search(li)
        if mp:
            price_raw = _strip_html(mp.group(1))

        items.append({"url": url, "title": title, "author": author, "price_raw": price_raw})

    # unique by url
    seen = set()
    out = []
    for it in items:
        u = it["url"]
        if u not in seen:
            seen.add(u)
            out.append(it)
    return out


def _discover_max_page(html: str, items_on_page: int) -> int:
    # 1) Por links (pagina=N)
    pages = [int(m.group(1)) for m in _RE_PAGINA.finditer(html or "") if m.group(1).isdigit()]
    if pages:
        return max(pages)

    # 2) Por "X libros encontrados" / items_per_page
    mt = _RE_TOTALES.search(html or "")
    if mt:
        txt = _strip_html(mt.group(1))
        mn = _RE_NUM_FOUND.search(txt)
        if mn and items_on_page > 0:
            total = int(mn.group(1))
            return max(1, ceil(total / items_on_page))

    return 1


def _score_match(query: str, title: str) -> float:
    return difflib.SequenceMatcher(None, (query or "").lower(), (title or "").lower()).ratio()


def _parse_dd(html: str, cls: str) -> str:
    m = _RE_DD(cls).search(html or "")
    return _strip_html(m.group(1)) if m else ""


def _parse_cover(html: str) -> str:
    m = _RE_COVER_HREF.search(html or "")
    if m:
        return urljoin(BASE + "/", m.group(1).strip())
    m = _RE_COVER_IMG.search(html or "")
    if m:
        return urljoin(BASE + "/", m.group(1).strip())
    return ""


def _extract_synopsis(html: str) -> str:
    m = _RE_DESC_BLOCK.search(html or "")
    if not m:
        return ""
    chunk = m.group(1)
    parts = [_strip_html(p) for p in _RE_P.findall(chunk)]
    parts = [p for p in parts if p]
    return "\n\n".join(parts).strip()


def _extract_author_asides(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for block in _RE_ASIDE_AUTOR.findall(html or ""):
        name = ""
        mn = _RE_ASIDE_H2.search(block)
        if mn:
            name = _strip_html(mn.group(1))

        bio = ""
        mp = _RE_ASIDE_P.search(block)
        if mp:
            bio = _strip_html(mp.group(1))

        foto = ""
        mi = _RE_ASIDE_IMG.search(block)
        if mi:
            foto = urljoin(BASE + "/", mi.group(1).strip())

        if name or bio or foto:
            out.append({"nombre": name, "bio": bio, "foto": foto})
    return out


def _parse_detail(html: str, url: str) -> Dict[str, Any]:
    titulo = ""
    mh1 = _RE_H1.search(html or "")
    if mh1:
        titulo = _strip_html(mh1.group(1))

    autor = ""
    ma = _RE_LI_AUTOR.search(html or "")
    if ma:
        autor = _strip_html(ma.group(1))

    editorial = ""
    me = _RE_LI_EDITOR.search(html or "")
    if me:
        editorial = _strip_html(me.group(1))

    isbn = _normalize_isbn(_parse_dd(html, "isbn"))
    precio = _parse_price_ars(_parse_dd(html, "precio"))
    tapa = _parse_dd(html, "tapa")
    paginas = _parse_dd(html, "paginas")
    dimensiones = _parse_dd(html, "dimensiones")
    portada = _parse_cover(html)
    descripcion = _extract_synopsis(html)
    autores_bio = _extract_author_asides(html)

    info_adicional: Dict[str, Any] = {
        "moneda": "ARS",
        "editorial": editorial,
        "tapa": tapa,
        "encuadernacion": tapa,
        "paginas": paginas,
        "dimensiones": dimensiones,
        "autores_bio": autores_bio,
    }

    return {
        "url_detalle": url,
        "titulo": titulo,
        "autor": autor,
        "isbn": isbn,
        "precio": precio,
        "descripcion": descripcion,
        "portada_url": portada,
        "info_adicional": info_adicional,
    }


def run_single(
    query: str,
    *,
    max_results: int = 4,
    timeout: float = 25.0,
    delay: float = 0.6,
    max_pages_cap: int = 8,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    headers = {"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9,en;q=0.8"}
    with httpx.Client(headers=headers) as client:
        # Atajo por ISBN: intentar directo por ean (sin t)
        if _is_isbnish(q):
            ean = _normalize_isbn(q)
            direct = f"{BASE}{DETAIL_PATH}?ean={ean}"
            try:
                html = _fetch_get(client, direct, timeout)
                row = _parse_detail(html, direct)
                if row.get("isbn") == ean and row.get("titulo"):
                    return [row]
            except Exception:
                pass

        # Buscar + autopaginación
        html1 = _search_page(client, q, 1, timeout)
        items1 = _parse_search_items(html1)
        if not items1:
            return []

        max_page = _discover_max_page(html1, len(items1))
        max_page = min(max_page, max_pages_cap)

        all_items = list(items1)
        for p in range(2, max_page + 1):
            time.sleep(delay)
            hp = _search_page(client, q, p, timeout)
            all_items.extend(_parse_search_items(hp))

        # scorings
        scored: List[Tuple[float, str]] = []
        if _is_isbnish(q):
            qn = _normalize_isbn(q)
            for it in all_items:
                if qn and qn in it["url"]:
                    scored.append((10.0, it["url"]))
        else:
            for it in all_items:
                scored.append((_score_match(q, it.get("title", "")), it["url"]))

        # unique + sort
        seen = set()
        uniq = []
        for sc, u in sorted(scored, key=lambda t: t[0], reverse=True):
            if u not in seen:
                seen.add(u)
                uniq.append((sc, u))

        results: List[Dict[str, Any]] = []
        for _, url in uniq[: max(12, max_results * 6)]:
            time.sleep(delay)
            try:
                dh = _fetch_get(client, url, timeout)
                row = _parse_detail(dh, url)
                if not row.get("titulo"):
                    continue
                if _is_isbnish(q):
                    qn = _normalize_isbn(q)
                    if row.get("isbn") and row["isbn"] != qn:
                        continue
                results.append(row)
                if len(results) >= max_results:
                    break
            except Exception:
                continue

        return results


def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    max_results: int = 4,
    timeout: float = 25.0,
    delay: float = 0.6,
    query_delay: float = 0.0,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    n = 0
    with open(query_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            q = raw.split("\t", 1)[0].split("|", 1)[0].strip()
            if not q:
                continue

            n += 1
            if limit_queries and n > limit_queries:
                break

            if query_delay and n > 1:
                time.sleep(query_delay)

            out.extend(
                run_single(
                    q,
                    max_results=max_results,
                    timeout=timeout,
                    delay=delay,
                    **kwargs,
                )
            )
    return out