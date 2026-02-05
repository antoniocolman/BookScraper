from __future__ import annotations

"""IberLibro / AbeBooks (iberlibro.com) scraper.

Estrategia:
- Buscar por query/ISBN vía /servlet/SearchResults?kn=...
- Parsear SRP (listado) para precio/moneda/vendedor/envío/URL detalle.
- (Opcional) Entrar al detalle (/.../bd) para metadata bibliográfica:
  editorial, idioma, encuadernación, estado, portada grande, etc.

Nota:
- Este módulo usa selectolax (según requirements del proyecto).
"""

import random
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

BASE_URL = "https://www.iberlibro.com"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_RE_WS = re.compile(r"\s+")
_RE_ISBN = re.compile(r"\b(97[89]\d{10}|\d{9}[\dXx])\b")

_BLOCK_MARKERS = (
    "verify that you're not a robot",
    "enable javascript",
    "javascript is disabled",
    "captcha",
)


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class QueryItem:
    raw: str
    isbn: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None


@dataclass
class Listing:
    url: str
    titulo: str = ""
    autor: str = ""
    isbn: str = ""
    precio: Optional[float] = None
    moneda: str = ""
    portada_url: str = ""
    descripcion: str = ""
    info: Dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Normalización
# -----------------------------------------------------------------------------

def _clean_text(s: Any) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\xa0", " ")
    s = _RE_WS.sub(" ", s).strip()
    return s


def normalize_isbn(x: Optional[str]) -> str:
    if not x:
        return ""
    return re.sub(r"[^0-9Xx]", "", str(x)).upper()


def _normalize_title(s: Optional[str]) -> str:
    if not s:
        return ""
    t = str(s).lower()
    t = (
        t.replace("á", "a").replace("é", "e").replace("í", "i")
         .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    )
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return _clean_text(t)


def _norm_label(s: str) -> str:
    t = _clean_text(s).lower()
    t = (
        t.replace("á", "a").replace("é", "e").replace("í", "i")
         .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    )
    return _RE_WS.sub(" ", t).strip()


def _simplify_binding(s: str) -> str:
    """Deja solo 'tapa dura' / 'tapa blanda' / etc."""
    t = _clean_text(s).lower()
    if not t:
        return ""
    if "tapa dura" in t or "hardcover" in t:
        return "tapa dura"
    if "tapa blanda" in t or "paperback" in t:
        return "tapa blanda"
    if "rustica" in t or "rústica" in t:
        return "rustica"
    # quitar prefijos comunes
    t = re.sub(r"^encuadernaci[oó]n\s+de\s+", "", t).strip()
    return t


# -----------------------------------------------------------------------------
# Lectura de consultas
# -----------------------------------------------------------------------------

def parse_query_line(line: str) -> Optional[QueryItem]:
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    # URL directa
    if raw.startswith("http://") or raw.startswith("https://"):
        return QueryItem(raw=raw, url=raw)

    # Soporta "ISBN | TITULO" o "ISBN<TAB>TITULO"
    sep = "|" if "|" in raw else ("\t" if "\t" in raw else None)
    left, right = raw, ""
    if sep:
        left, right = raw.split(sep, 1)
        left, right = left.strip(), right.strip()

    m = _RE_ISBN.search(left) or _RE_ISBN.search(raw)
    isbn = normalize_isbn(m.group(1)) if m else None
    title = right if right else (None if isbn else raw)

    return QueryItem(raw=raw, isbn=isbn, title=title)


def load_queries_from_file(path: Path, limit: Optional[int] = None) -> List[QueryItem]:
    if not path.is_file():
        return []
    out: List[QueryItem] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            q = parse_query_line(line)
            if not q:
                continue
            out.append(q)
            if limit is not None and len(out) >= limit:
                break
    return out


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

def make_client() -> httpx.Client:
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
        "Referer": BASE_URL + "/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    return httpx.Client(headers=headers, follow_redirects=True, timeout=30.0, http2=True)


def _fetch_html_with_backoff(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int = 3,
    base_wait: float = 6.0,
) -> str:
    last: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            r = client.get(url)
            if r.status_code in (429, 503):
                raise httpx.HTTPStatusError("rate limited", request=r.request, response=r)
            r.raise_for_status()
            return r.text or ""
        except Exception as e:
            last = e
            if attempt >= max_retries:
                break
            sleep_s = min(120.0, base_wait * (2 ** attempt)) + random.uniform(0.2, 1.2)
            time.sleep(sleep_s)
    raise last or RuntimeError("fetch failed")


def _looks_blocked(html: str) -> bool:
    low = (html or "").lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _build_search_url(q: str) -> str:
    return f"{BASE_URL}/servlet/SearchResults?kn={quote_plus(q)}&ds=20"


# -----------------------------------------------------------------------------
# Parsing SRP (listado)
# -----------------------------------------------------------------------------

def _parse_price(num_str: str) -> Optional[float]:
    """Soporta '26.95', '26,95', '1.234,56'."""
    s = _clean_text(num_str)
    if not s:
        return None
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        if "," in s and re.search(r",\d{2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


def _node_attr(node, key: str) -> str:
    if not node:
        return ""
    return (node.attributes.get(key) or "").strip()


def parse_search_results(html: str, *, max_results: int = 0) -> List[Listing]:
    doc = HTMLParser(html or "")
    items = doc.css('ul#srp-results li[data-test-id="listing-item"]')
    if max_results and max_results > 0:
        items = items[:max_results]

    out: List[Listing] = []
    for li in items:
        meta_isbn = li.css_first('meta[itemprop="isbn"]')
        meta_title = li.css_first('meta[itemprop="name"]')
        meta_author = li.css_first('meta[itemprop="author"]')
        meta_pub = li.css_first('meta[itemprop="publisher"]')
        meta_year = li.css_first('meta[itemprop="datePublished"]')

        meta_price = li.css_first('meta[itemprop="price"]')
        meta_curr = li.css_first('meta[itemprop="priceCurrency"]')

        a_url = li.css_first('a[itemprop="url"]')
        img = li.css_first('img.srp-item-image')

        title_span = li.css_first('span[data-test-id="listing-title"]')
        author_strong = li.css_first('p[data-test-id="listing-author"] strong')

        desc = li.css_first('p[data-test-id="listing-description"]')
        cond = li.css_first('span[data-test-id="listing-book-condition"]')
        lang = li.css_first('p[data-test-id="listing-language"]')
        seller = li.css_first('span[data-test-id="listing-seller-name"]')
        seller_loc = li.css_first('span[data-test-id="listing-seller-location"]')
        shipping = li.css_first('span[data-test-id="shipping-detail"]')

        url = urljoin(BASE_URL, _node_attr(a_url, 'href')) if a_url else ""
        portada = _node_attr(img, 'src') if img else ""

        titulo = _node_attr(meta_title, 'content') or (title_span.text().strip() if title_span else "")
        autor = _node_attr(meta_author, 'content') or (author_strong.text().strip() if author_strong else "")
        isbn = normalize_isbn(_node_attr(meta_isbn, 'content'))
        precio = _parse_price(_node_attr(meta_price, 'content')) if meta_price else None
        moneda = _node_attr(meta_curr, 'content')

        info: Dict[str, Any] = {}
        if meta_pub and _node_attr(meta_pub, 'content'):
            info['editorial'] = _node_attr(meta_pub, 'content')
        if meta_year and _node_attr(meta_year, 'content'):
            info['fecha_publicacion'] = _node_attr(meta_year, 'content')
        if cond:
            info['condicion'] = _clean_text(cond.text())
        if lang:
            info['idioma'] = _clean_text(lang.text()).replace('Idioma:', '').strip()
        if seller:
            info['vendedor'] = _clean_text(seller.text())
        if seller_loc:
            info['vendedor_ubicacion'] = _clean_text(seller_loc.text())
        if shipping:
            info['envio_raw'] = _clean_text(shipping.text())

            # intenta extraer moneda/monto del envío (best-effort)
            m = re.search(r"Envío\s+por\s+([A-Z]{3})\s*([\d.,]+)", info['envio_raw'])
            if m:
                info['envio_moneda'] = m.group(1)
                info['envio_precio'] = _parse_price(m.group(2))

        descripcion = _clean_text(desc.text()) if desc else ""

        out.append(
            Listing(
                url=url,
                titulo=_clean_text(titulo),
                autor=_clean_text(autor),
                isbn=isbn,
                precio=precio,
                moneda=_clean_text(moneda),
                portada_url=_clean_text(portada),
                descripcion=descripcion,
                info=info,
            )
        )

    return out


# -----------------------------------------------------------------------------
# Parsing detalle (/bd)
# -----------------------------------------------------------------------------

def parse_detail_page(html: str, url: str) -> Dict[str, Any]:
    doc = HTMLParser(html or "")

    # portada grande
    portada = ""
    img = doc.css_first('img#isbn-image')
    if img:
        portada = urljoin(url, _node_attr(img, 'src'))

    # autor
    autor = ""
    a = doc.css_first('h2#book-author a') or doc.css_first('h2[data-test-id="book-author"] a')
    if a:
        autor = _clean_text(a.text())

    # metadata dt/dd
    meta_map: Dict[str, str] = {}
    dl = doc.css_first('dl.listing-metadata')
    if dl:
        dts = dl.css('dt')
        dds = dl.css('dd')
        for dt, dd in zip(dts, dds):
            k = _norm_label(dt.text())
            v = _clean_text(dd.text())
            if k:
                meta_map[k] = v

    titulo = meta_map.get('titulo', '')
    editorial = meta_map.get('editor', '')
    fecha = meta_map.get('fecha de publicacion', '')
    idioma = meta_map.get('idioma', '')
    isbn10 = normalize_isbn(meta_map.get('isbn 10', ''))
    isbn13 = normalize_isbn(meta_map.get('isbn 13', ''))
    enc = _simplify_binding(meta_map.get('encuadernacion', ''))
    estado = meta_map.get('estado', '')

    return {
        'url_detalle': url,
        'titulo': titulo,
        'autor': autor,
        'editorial': editorial,
        'fecha_publicacion': fecha,
        'idioma': idioma,
        'isbn10': isbn10,
        'isbn13': isbn13,
        'encuadernacion': enc,
        'estado': estado,
        'portada_url': portada,
        'raw_meta': meta_map,
    }


def _is_detail_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.path.endswith('/bd') or re.search(r"/\d+/bd$", p.path) is not None
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Selección de mejor candidato
# -----------------------------------------------------------------------------

def pick_best_candidate(query: QueryItem, listings: List[Listing]) -> Optional[Listing]:
    if not listings:
        return None

    q_isbn = normalize_isbn(query.isbn) if query.isbn else ""
    q_title = _normalize_title(query.title) if query.title else ""

    best: Optional[Listing] = None
    best_score = -1.0

    for it in listings:
        score = 0.0
        if q_isbn and it.isbn and it.isbn == q_isbn:
            score += 100.0

        if q_title:
            t_norm = _normalize_title(it.titulo)
            if t_norm == q_title:
                score += 20.0
            if t_norm and (q_title in t_norm or t_norm in q_title):
                score += 5.0
            score += SequenceMatcher(None, q_title, t_norm).ratio()

        if it.precio is not None:
            score += 0.1
        if it.url:
            score += 0.1

        if score > best_score:
            best_score = score
            best = it

    return best or listings[0]


# -----------------------------------------------------------------------------
# API principal (usada por wrappers de site)
# -----------------------------------------------------------------------------

def search_iberlibro(
    client: httpx.Client,
    query: QueryItem,
    *,
    delay: float = 1.5,
    max_retries: int = 3,
    base_wait: float = 6.0,
    max_results: int = 0,
    fetch_detail: bool = True,
    detail_delay: float = 0.7,
) -> Optional[Dict[str, Any]]:
    """Busca en IberLibro.

    Devuelve un dict 'row' compatible con el resto del pipeline.
    """

    # Si es URL directa a detalle, parsear detalle directamente (best-effort)
    if query.url and _is_detail_url(query.url):
        if delay > 0:
            time.sleep(delay + random.uniform(0.1, 0.35))
        html = _fetch_html_with_backoff(client, query.url, max_retries=max_retries, base_wait=base_wait)
        if _looks_blocked(html):
            return {
                "sitio": "iberlibro",
                "query_raw": query.raw,
                "url_detalle": query.url,
                "titulo": "",
                "autor": "",
                "isbn": normalize_isbn(query.isbn) if query.isbn else "",
                "precio": None,
                "moneda": "",
                "portada_url": "",
                "descripcion": "",
                "info_adicional": {"blocked": True, "reason": "verification_or_js"},
            }
        d = parse_detail_page(html, query.url)
        return {
            "sitio": "iberlibro",
            "query_raw": query.raw,
            "url_detalle": d.get("url_detalle", query.url),
            "titulo": d.get("titulo", ""),
            "autor": d.get("autor", ""),
            "isbn": d.get("isbn13") or d.get("isbn10") or (normalize_isbn(query.isbn) if query.isbn else ""),
            "precio": None,
            "moneda": "",
            "portada_url": d.get("portada_url", ""),
            "descripcion": "",
            "info_adicional": {
                "editorial": d.get("editorial", ""),
                "fecha_publicacion": d.get("fecha_publicacion", ""),
                "idioma": d.get("idioma", ""),
                "encuadernacion": d.get("encuadernacion", ""),
                "estado": d.get("estado", ""),
                "raw_meta": d.get("raw_meta", {}),
            },
        }

    # SearchResults URL o búsqueda por query
    url = query.url if (query.url and "SearchResults" in query.url) else _build_search_url(query.title or query.isbn or query.raw)

    if delay > 0:
        time.sleep(delay + random.uniform(0.1, 0.35))

    html = _fetch_html_with_backoff(client, url, max_retries=max_retries, base_wait=base_wait)
    if _looks_blocked(html):
        return {
            "sitio": "iberlibro",
            "query_raw": query.raw,
            "url_detalle": url,
            "titulo": "",
            "autor": "",
            "isbn": normalize_isbn(query.isbn) if query.isbn else "",
            "precio": None,
            "moneda": "",
            "portada_url": "",
            "descripcion": "",
            "info_adicional": {"blocked": True, "reason": "verification_or_js"},
        }

    listings = parse_search_results(html, max_results=max_results)
    best = pick_best_candidate(query, listings)
    if not best:
        return None

    # base row desde SRP
    row: Dict[str, Any] = {
        "sitio": "iberlibro",
        "query_raw": query.raw,
        "url_detalle": best.url or url,
        "titulo": best.titulo,
        "autor": best.autor,
        "isbn": best.isbn or (normalize_isbn(query.isbn) if query.isbn else ""),
        "precio": best.precio,
        "moneda": best.moneda,
        "portada_url": best.portada_url,
        "descripcion": best.descripcion,
        "info_adicional": dict(best.info or {}),
    }

    # opcional: detalle
    if fetch_detail and best.url:
        try:
            if detail_delay > 0:
                time.sleep(detail_delay + random.uniform(0.05, 0.25))

            detail_html = _fetch_html_with_backoff(client, best.url, max_retries=max_retries, base_wait=base_wait)
            if not _looks_blocked(detail_html):
                d = parse_detail_page(detail_html, best.url)

                # merge: preferir detalle
                if d.get('titulo'):
                    row['titulo'] = d['titulo']
                if d.get('autor'):
                    row['autor'] = d['autor']
                row['portada_url'] = d.get('portada_url') or row.get('portada_url', '')
                row['isbn'] = d.get('isbn13') or d.get('isbn10') or row.get('isbn', '')

                info = row.get('info_adicional') or {}
                # no pisar si ya venía (pero detalle suele ser más confiable)
                info['editorial'] = d.get('editorial') or info.get('editorial', '')
                info['fecha_publicacion'] = d.get('fecha_publicacion') or info.get('fecha_publicacion', '')
                info['idioma'] = d.get('idioma') or info.get('idioma', '')
                if d.get('encuadernacion'):
                    info['encuadernacion'] = d['encuadernacion']
                if d.get('estado'):
                    info['estado'] = d['estado']
                # guardar raw_meta por si hace falta debugging
                info.setdefault('raw_meta', d.get('raw_meta', {}))
                row['info_adicional'] = info
        except Exception as e:
            # best-effort: no romper el pipeline
            info = row.get('info_adicional') or {}
            info['detail_error'] = str(e)
            row['info_adicional'] = info

    return row


# Compat con wrappers existentes:
def fetch_and_parse(
    client: httpx.Client,
    q: QueryItem,
    *,
    delay: float = 1.5,
    max_retries: int = 3,
    base_wait: float = 6.0,
    max_results: int = 0,
    fetch_detail: bool = True,
) -> Optional[Dict[str, Any]]:
    return search_iberlibro(
        client,
        q,
        delay=delay,
        max_retries=max_retries,
        base_wait=base_wait,
        max_results=max_results,
        fetch_detail=fetch_detail,
    )
