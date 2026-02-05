#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Casa del Libro - Experimental

MVP estable:
- Search por API Empathy:
    - ISBN:  /search/v1/query/cdl/isbnsearch
    - Texto: /search/v1/query/cdl/search?facets=false
- Parse HTML (selectolax) para:
    - Breadcrumb/categoría
    - Sinopsis
    - Ficha técnica (data-campo)

Notas:
- Evitamos depender de clases svelte-*.
- Para campos conflictivos en HTML (autor/portada/precio), usamos API como fuente principal cuando esté disponible.
- La URL de producto suele ser:
    https://www.casadellibro.com/libro-.../<isbn13>/<id>
    https://www.casadellibro.com/ebook-.../<isbn13>/<id>

CLI:
  python casa_del_libro_experimental.py --version
  python casa_del_libro_experimental.py search --q "9788408283539"
  python casa_del_libro_experimental.py search --q "spiderman" --limit 10
  python casa_del_libro_experimental.py product --url "https://www.casadellibro.com/libro-.../978.../123"
"""

from __future__ import annotations

__VERSION__ = "2026-01-27.4"

import argparse
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import httpx

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser  # type: ignore
except Exception:  # pragma: no cover
    HTMLParser = None  # type: ignore


BASE_URL = "https://www.casadellibro.com"
API_BASE = "https://api.empathy.co"

API_SEARCH = f"{API_BASE}/search/v1/query/cdl/search"
API_ISBNSEARCH = f"{API_BASE}/search/v1/query/cdl/isbnsearch"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}

_RE_WS = re.compile(r"\s+")
_RE_ISBN = re.compile(r"\b(97[89]\d{10}|\d{9}[\dXx])\b")
_RE_EUR = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€")
_RE_ZW = re.compile(r"[\u200b-\u200f\uFEFF]")  # zero-width + BOM


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def clean_text(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).replace("\xa0", " ")
    t = _RE_ZW.sub("", t)
    t = _RE_WS.sub(" ", t).strip()
    return t


def normalize_isbn(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).upper()
    s = _RE_ZW.sub("", s)
    s = re.sub(r"[^0-9X]", "", s)
    return s


def is_isbnish(s: str) -> bool:
    x = normalize_isbn(s)
    return len(x) in (10, 13) and bool(_RE_ISBN.search(x))


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    p = urlparse(u)
    p2 = p._replace(query="", fragment="")
    return urlunparse(p2)


def sleep_delay(base: float) -> None:
    if base <= 0:
        return
    time.sleep(float(base) + random.uniform(0.10, 0.35))


def parse_price_text_eur(t: str) -> Optional[float]:
    """Convierte '15,15 €' -> 15.15"""
    s = clean_text(t).replace("€", "").strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def pick_best_from_srcset(src: str, srcset: str) -> str:
    """Elige la URL de mayor 'w' del srcset. Fallback a src."""
    best_url = (src or "").strip()
    best_w = -1
    for part in (srcset or "").split(","):
        part = part.strip()
        if not part:
            continue
        chunks = part.split()
        url = chunks[0].strip()
        w = -1
        if len(chunks) > 1 and chunks[1].endswith("w"):
            try:
                w = int(chunks[1][:-1])
            except Exception:
                w = -1
        if w > best_w:
            best_w = w
            best_url = url
    return best_url


def _dedupe_repeat_phrase(val: str) -> str:
    """
    Quita duplicaciones típicas por tooltip:
      - 'Ficción Ficción'
      - 'FicciónFicción'
      - 'Ficción\u200bFicción'
    """
    v = clean_text(val)
    if not v:
        return v

    toks = v.split()
    n = len(toks)
    if n >= 2 and n % 2 == 0:
        half = n // 2
        if toks[:half] == toks[half:]:
            return " ".join(toks[:half])

    # repetición sin espacios
    v2 = v.replace(" ", "")
    if len(v2) >= 2 and len(v2) % 2 == 0:
        half = len(v2) // 2
        if v2[:half] == v2[half:]:
            # intentar devolver con espacios (mejor UX), si no, el half plano
            return v2[:half]

    # último fallback: si hay una palabra repetida pegada
    m = re.match(r"^(.+?)\1$", v2)
    if m:
        return m.group(1)

    return v


def extract_isbn_from_url(url: str) -> str:
    """
    URL típica:
      /libro-.../<isbn13>/<id>
      /ebook-.../<isbn13>/<id>
    """
    p = urlparse(url).path
    parts = [x for x in p.split("/") if x]
    if len(parts) >= 3:
        maybe = parts[-2]
        if maybe and maybe[0].isdigit():
            return normalize_isbn(maybe)
    m = _RE_ISBN.search(url)
    return normalize_isbn(m.group(1)) if m else ""


# -----------------------------------------------------------------------------
# HTTP + API
# -----------------------------------------------------------------------------

def get_client() -> httpx.Client:
    return httpx.Client(
        headers=dict(DEFAULT_HEADERS),
        http2=True,
        follow_redirects=True,
        timeout=30.0,
    )


def _api_params(query: str, *, start: int, rows: int) -> Dict[str, Any]:
    return {
        "query": query,
        "origin": "search_box:none",
        "start": int(start),
        "rows": int(rows),
        "instance": "cdl",
        "lang": "es",
        "scope": "desktop",
        "currency": "EUR",
        "store": "ES",
    }


def api_search(client: httpx.Client, query: str, *, start: int = 0, rows: int = 24) -> Dict[str, Any]:
    r = client.get(API_SEARCH, params={**_api_params(query, start=start, rows=rows), "facets": "false"})
    r.raise_for_status()
    return r.json()


def api_isbnsearch(client: httpx.Client, isbn: str, *, start: int = 0, rows: int = 24) -> Dict[str, Any]:
    r = client.get(API_ISBNSEARCH, params=_api_params(isbn, start=start, rows=rows))
    r.raise_for_status()
    return r.json()


def iter_search_results(
    client: httpx.Client,
    query: str,
    *,
    limit_pages: int = 1,
    rows: int = 24,
    delay: float = 0.0,
) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (product_url, raw_item_json)."""
    q = clean_text(query)
    if not q:
        return

    want_isbn = is_isbnish(q)
    q_isbn = normalize_isbn(q) if want_isbn else ""

    for page in range(max(1, int(limit_pages))):
        start = page * int(rows)
        data = api_isbnsearch(client, q_isbn, start=start, rows=rows) if want_isbn else api_search(client, q, start=start, rows=rows)

        items = (((data or {}).get("catalog") or {}).get("content") or []) if isinstance(data, dict) else []
        for it in items:
            url = clean_text(it.get("url") or it.get("__url") or "")
            if url:
                url = canonicalize_url(url)
            if not url:
                continue

            if want_isbn:
                ean = normalize_isbn(it.get("ean") or it.get("isbn") or "")
                if ean and ean == q_isbn:
                    yield url, it
                continue

            yield url, it

        if delay:
            sleep_delay(delay)


def api_item_for_product_url(client: httpx.Client, product_url: str) -> Optional[Dict[str, Any]]:
    """
    Obtiene el item API correspondiente a una ficha:
    - extrae ISBN de la URL
    - llama isbnsearch y matchea por url exacta (o ean)
    """
    u = canonicalize_url(product_url)
    isbn = extract_isbn_from_url(u)
    if not isbn:
        return None

    def _pick(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not items:
            return None
        # match exacto por url, luego por ean
        for it in items:
            url = canonicalize_url(clean_text(it.get("url") or it.get("__url") or ""))
            if url and url == u:
                return it
        for it in items:
            ean = normalize_isbn(it.get("ean") or it.get("isbn") or "")
            if ean and ean == isbn:
                return it
        return items[0]

    try:
        data = api_isbnsearch(client, isbn, start=0, rows=24)
        items = (((data or {}).get("catalog") or {}).get("content") or [])
        pick = _pick(items)
        if pick:
            return pick
    except Exception:
        pass

    # fallback: search por ISBN (a veces incluye ebooks o variantes)
    try:
        data = api_search(client, isbn, start=0, rows=24)
        items = (((data or {}).get("catalog") or {}).get("content") or [])
        return _pick(items)
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Modelo
# -----------------------------------------------------------------------------

@dataclass
class CasaDelLibroBook:
    site: str
    url: str

    titulo: Optional[str] = None
    autor: Optional[str] = None
    editorial: Optional[str] = None
    isbn: Optional[str] = None

    idioma: Optional[str] = None
    paginas: Optional[str] = None
    dimensiones: Optional[str] = None
    peso: Optional[str] = None

    fecha_publicacion: Optional[str] = None
    categoria: Optional[str] = None

    descripcion: Optional[str] = None  # sinopsis
    url_portada: Optional[str] = None

    precio: Optional[float] = None
    moneda: Optional[str] = "EUR"
    encuadernacion: Optional[str] = None

    categoria_path: Optional[List[str]] = None

    info_adicional: Optional[Dict[str, Any]] = None
    raw_api: Optional[Dict[str, Any]] = None


# -----------------------------------------------------------------------------
# HTML parsing (selectolax)
# -----------------------------------------------------------------------------

def _parse_breadcrumb(tree: Any) -> Tuple[List[str], str]:
    parts: List[str] = []
    ol = None
    home = tree.css_first('ol li[aria-label="home"]')
    if home is not None:
        ol = home.parent
        while ol is not None and getattr(ol, "tag", "") != "ol":
            ol = ol.parent
    if ol is None:
        ol = tree.css_first("ol")
    if ol is None:
        return [], ""

    for a in ol.css("li a"):
        txt = clean_text(a.text(strip=True))
        if txt:
            parts.append(txt)

    return parts, (parts[-1] if parts else "")


def _parse_synopsis(tree: Any) -> str:
    # preferimos el resumen-content con más texto
    nodes = tree.css("div.resumen-content")
    if not nodes:
        return ""
    best = max(nodes, key=lambda n: len(clean_text(n.text(strip=True))))
    ps = [clean_text(p.text(strip=True)) for p in best.css("p")]
    ps = [p for p in ps if p]
    return "\n".join(ps).strip()


def _parse_cover(tree: Any, *, isbn: str = "") -> str:
    # 1) buscar imagen que contenga el ISBN en src/srcset/alt
    if isbn:
        for img in tree.css("img"):
            src = img.attributes.get("src", "") or ""
            srcset = img.attributes.get("srcset", "") or ""
            alt = img.attributes.get("alt", "") or ""
            hay = f"{src} {srcset} {alt}"
            if isbn in hay:
                return pick_best_from_srcset(src, srcset)

    # 2) fallback a la primera dentro de cdl-img
    img = tree.css_first("cdl-img img") or tree.css_first("img[src*='casadellibro']")
    if img is None:
        return ""
    src = img.attributes.get("src", "") or ""
    srcset = img.attributes.get("srcset", "") or ""
    return pick_best_from_srcset(src, srcset)


def _parse_selected_price_and_binding(tree: Any) -> Tuple[Optional[float], Optional[str]]:
    """
    En ficha: .producto-asociado.selected
    HTML a veces es inválido (<p> dentro de <p>), así que usamos heurística de texto.
    """
    sel = tree.css_first(".producto-asociado.selected") or tree.css_first(".producto-asociado[aria-current='true']")
    if sel is None:
        return None, None

    txt = clean_text(sel.text(separator=" ", strip=True))
    # binding: heurística por tokens
    binding = None
    for b in ("Tapa blanda", "Tapa dura", "Bolsillo", "Ebook", "eBook", "Otros"):
        if b.lower() in txt.lower():
            binding = "Ebook" if b.lower() in ("ebook", "ebook") else b
            break

    m = _RE_EUR.search(txt)
    price = parse_price_text_eur(m.group(0)) if m else None

    # fallback: si no detectó binding, usar el primer <p> sin €
    if binding is None:
        for p in sel.css("p"):
            t = clean_text(p.text(strip=True))
            if t and "€" not in t and len(t) < 60:
                binding = t
                break

    return price, binding


def _parse_ficha_tecnica(tree: Any) -> Dict[str, str]:
    ficha = tree.css_first("div.ficha-tecnica#contributors") or tree.css_first("div.ficha-tecnica")
    if ficha is None:
        return {}

    out: Dict[str, str] = {}
    for node in ficha.css("[data-campo]"):
        key = clean_text(node.attributes.get("data-campo", ""))
        if not key:
            continue

        # remover tooltips si existen (reduce duplicaciones)
        for t in node.css(".tooltip, .tooltip-content"):
            try:
                t.decompose()
            except Exception:
                pass

        label = ""
        b = node.css_first("b")
        if b is not None:
            label = clean_text(b.text(strip=True))

        full = clean_text(node.text(separator=" ", strip=True))
        val = full
        if label and full.startswith(label):
            val = clean_text(full[len(label):])

        val = _dedupe_repeat_phrase(val)

        # Serie/Saga: además guardar URL si hay <a>
        if key.lower() in ("serie/saga", "serie", "saga"):
            a = node.css_first("a")
            if a is not None:
                out["Serie/Saga"] = clean_text(a.text(strip=True))
                href = clean_text(a.attributes.get("href", ""))
                if href:
                    out["Serie/Saga URL"] = href
                continue

        out[key] = val

    return out


def _parse_author_scoped(tree: Any) -> str:
    """
    Autor en ficha: suele estar cerca del título (#t-p-f).
    Evita capturar autores de sliders/recomendados.
    """
    tnode = tree.css_first("#t-p-f")
    if tnode is None:
        return ""

    bad = ("ver más", "descubre más", "descubre", "ver", "más")
    container = tnode.parent
    for _ in range(0, 6):
        if container is None:
            break
        links = container.css("a[href^='/libros-ebooks/']")
        authors: List[str] = []
        for a in links:
            txt = clean_text(a.text(strip=True))
            if not txt:
                continue
            low = txt.lower()
            if any(x in low for x in bad):
                continue
            if len(txt) > 60:
                continue
            authors.append(txt)

        # dedupe preservando orden
        seen = set()
        authors = [x for x in authors if not (x in seen or seen.add(x))]
        if authors:
            return ", ".join(authors)

        container = container.parent

    return ""


def parse_product_html(html: str, url: str) -> Dict[str, Any]:
    if HTMLParser is None:
        raise RuntimeError("selectolax no está disponible (LexborHTMLParser). Instalá selectolax.")

    tree = HTMLParser(html)

    isbn_from_url = extract_isbn_from_url(url)

    # título
    tnode = tree.css_first("#t-p-f")
    titulo = clean_text(tnode.text(strip=True)) if tnode is not None else ""

    autor = _parse_author_scoped(tree)

    categoria_path, categoria = _parse_breadcrumb(tree)
    sinopsis = _parse_synopsis(tree)
    portada = _parse_cover(tree, isbn=isbn_from_url)
    price, binding = _parse_selected_price_and_binding(tree)
    ficha = _parse_ficha_tecnica(tree)

    alto = ficha.get("Alto", "")
    ancho = ficha.get("Ancho", "")
    grueso = ficha.get("Grueso", "")
    dimensiones = f"{alto} x {ancho} x {grueso}" if (alto and ancho and grueso) else ""

    return {
        "titulo": titulo,
        "autor": autor,
        "sinopsis": sinopsis,
        "url_portada": portada,
        "precio": price,
        "encuadernacion": binding or ficha.get("Encuadernación", ""),
        "categoria_path": categoria_path,
        "categoria": categoria,
        "ficha": ficha,
        "dimensiones": dimensiones,
    }


# -----------------------------------------------------------------------------
# Merge API + HTML
# -----------------------------------------------------------------------------

def _api_author(item: Dict[str, Any]) -> str:
    if not item:
        return ""
    a1 = clean_text(item.get("author1") or "")
    if a1:
        return a1
    arr = item.get("authors")
    if isinstance(arr, list):
        seen = set()
        out = []
        for x in arr:
            t = clean_text(x)
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return ", ".join(out)
    return ""


def _api_cover(item: Dict[str, Any]) -> str:
    if not item:
        return ""
    imgs = item.get("__images")
    if isinstance(imgs, list) and imgs:
        return clean_text(imgs[0])
    return ""


def _api_price(item: Dict[str, Any]) -> Optional[float]:
    if not item:
        return None
    po = item.get("priceOffer")
    if isinstance(po, (int, float)):
        return float(po)
    pr = item.get("price") or {}
    if isinstance(pr, dict):
        cur = pr.get("current")
        if isinstance(cur, (int, float)):
            return float(cur)
    return None


def fetch_html(client: httpx.Client, url: str, *, timeout: float = 30.0) -> str:
    r = client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_and_parse_product(client: httpx.Client, url: str, *, timeout: float = 30.0) -> CasaDelLibroBook:
    u = canonicalize_url(url)

    api_item = api_item_for_product_url(client, u)

    html = fetch_html(client, u, timeout=timeout)
    parsed = parse_product_html(html, u)
    ficha = parsed.get("ficha") or {}

    # ISBN
    isbn = normalize_isbn(ficha.get("ISBN") or "")
    if not isbn and api_item:
        isbn = normalize_isbn(api_item.get("ean") or api_item.get("isbn") or "")
    if not isbn:
        isbn = extract_isbn_from_url(u)

    # editorial
    editorial = clean_text(ficha.get("Editorial") or "")
    if not editorial and api_item:
        editorial = clean_text(api_item.get("editorial") or "")

    # autor / título (API primero, HTML fallback)
    titulo = clean_text(api_item.get("__name") or api_item.get("name") or "") if api_item else ""
    if not titulo:
        titulo = clean_text(parsed.get("titulo") or "")
    autor = _api_author(api_item) if api_item else ""
    if not autor:
        autor = clean_text(parsed.get("autor") or "")

    # precio / encuadernación
    precio = parsed.get("precio")
    if precio is None and api_item:
        precio = _api_price(api_item)

    enc = clean_text(parsed.get("encuadernacion") or "")
    if not enc and api_item:
        enc = clean_text(api_item.get("encuadernation") or "")

    # portada (API primero si existe)
    portada = _api_cover(api_item) if api_item else ""
    if not portada:
        portada = clean_text(parsed.get("url_portada") or "")

    # idioma/páginas/fecha
    idioma = clean_text(ficha.get("Idioma") or "")
    paginas = clean_text(ficha.get("Número de páginas") or "")
    fecha = clean_text(ficha.get("Fecha de lanzamiento") or "")
    peso = clean_text(ficha.get("Peso") or "")
    dimensiones = clean_text(parsed.get("dimensiones") or "")

    # descripción (API a veces trae mini, HTML suele ser mejor)
    descripcion = clean_text(parsed.get("sinopsis") or "")
    if not descripcion and api_item:
        descripcion = clean_text(api_item.get("description") or "")

    return CasaDelLibroBook(
        site="casa_del_libro",
        url=u,
        titulo=titulo or None,
        autor=autor or None,
        editorial=editorial or None,
        isbn=isbn or None,
        idioma=idioma or None,
        paginas=paginas or None,
        dimensiones=dimensiones or None,
        peso=peso or None,
        fecha_publicacion=fecha or None,
        categoria=clean_text(parsed.get("categoria") or "") or None,
        descripcion=descripcion or None,
        url_portada=portada or None,
        precio=precio,
        moneda="EUR",
        encuadernacion=enc or None,
        categoria_path=parsed.get("categoria_path") or None,
        info_adicional=ficha or None,
        raw_api=api_item or None,
    )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _cmd_search(args: argparse.Namespace) -> int:
    c = get_client()
    count = 0
    for url, _it in iter_search_results(c, args.q, limit_pages=args.pages, rows=args.rows, delay=args.delay):
        print(url)
        count += 1
        if args.limit and count >= args.limit:
            break
    return 0


def _cmd_product(args: argparse.Namespace) -> int:
    c = get_client()
    b = fetch_and_parse_product(c, args.url, timeout=args.timeout)
    print(asdict(b))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="store_true", help="Print version and exit.")

    sub = ap.add_subparsers(dest="cmd", required=False)

    ap_s = sub.add_parser("search")
    ap_s.add_argument("--q", required=True)
    ap_s.add_argument("--pages", type=int, default=1)
    ap_s.add_argument("--rows", type=int, default=24)
    ap_s.add_argument("--limit", type=int, default=10)
    ap_s.add_argument("--delay", type=float, default=0.0)
    ap_s.set_defaults(fn=_cmd_search)

    ap_p = sub.add_parser("product")
    ap_p.add_argument("--url", required=True)
    ap_p.add_argument("--timeout", type=float, default=30.0)
    ap_p.set_defaults(fn=_cmd_product)

    args = ap.parse_args(argv)

    if getattr(args, "version", False):
        print(__VERSION__)
        return 0

    if not getattr(args, "cmd", None):
        ap.print_help()
        return 2

    return int(args.fn(args))

if __name__ == "__main__":
    raise SystemExit(main())
