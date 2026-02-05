#!/usr/bin/env python3
"""
Casa del Libro (experimental)

CLI:
  python casa_del_libro_experimental.py search --q "9788408283539" [--rows 24]
  python casa_del_libro_experimental.py product --url "https://www.casadellibro.com/libro-.../978.../14434118"

Notas:
- Search usa API Empathy (api.empathy.co).
- Product usa API por ISBN para autor/precio/portada (robusto) + HTML para ficha técnica/breadcrumb/sinopsis.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

SITE = "casa_del_libro"
BASE = "https://www.casadellibro.com"
API_BASE = "https://api.empathy.co/search/v1/query/cdl"

# --- utils -------------------------------------------------------------------

_RE_WS = re.compile(r"\s+")
_RE_PRICE = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+(?:\.[0-9]{2})?)")

def clean_text(s: str) -> str:
    return _RE_WS.sub(" ", (s or "").replace("\xa0", " ")).strip()

def is_isbn_query(q: str) -> bool:
    qn = re.sub(r"[^0-9Xx]", "", q or "")
    if len(qn) == 10:
        return True
    if len(qn) == 13 and qn.isdigit():
        return True
    return False

def extract_isbn_from_url(url: str) -> str:
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return ""
    parts = [p for p in path.split("/") if p]
    # Expected: /libro-.../<ean>/<id> OR /ebook-.../<ean>/<id>
    if len(parts) >= 3:
        cand = parts[-2]
        cand = re.sub(r"[^0-9Xx]", "", cand)
        if cand:
            return cand
    return ""

def dedupe_double(value: str) -> str:
    """Fix duplicated values caused by tooltip duplication.
    Examples: 'FicciónFicción' or 'Ficción Ficción' -> 'Ficción'.
    """
    v = clean_text(value)
    if not v:
        return v

    # Token-based duplication: 'A B A B' => 'A B'
    toks = v.split()
    if len(toks) >= 2 and len(toks) % 2 == 0:
        half = len(toks) // 2
        if toks[:half] == toks[half:]:
            return " ".join(toks[:half])

    # Exact string duplication: 'XYZXYZ' => 'XYZ'
    if len(v) % 2 == 0:
        half_s = v[: len(v)//2]
        if half_s == v[len(v)//2 :]:
            return half_s

    # Generic repetition
    m = re.match(r"^(.+?)\1$", v)
    if m:
        return clean_text(m.group(1))

    return v

def parse_price_to_float(text: str) -> Optional[float]:
    t = clean_text(text)
    if not t:
        return None
    m = _RE_PRICE.search(t)
    if not m:
        return None
    num = m.group(1)
    # Spanish format: 1.234,56
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        return float(num)
    except Exception:
        return None

def abs_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    return BASE + "/" + href

def best_img_from_srcset(img_node) -> str:
    if not img_node:
        return ""
    src = img_node.attributes.get("src") or ""
    srcset = img_node.attributes.get("srcset") or ""
    best = src
    best_w = -1
    for part in srcset.split(","):
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
            best = url
    return best or src

# --- http --------------------------------------------------------------------

def make_client() -> httpx.Client:
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    }
    return httpx.Client(headers=headers, timeout=30.0, follow_redirects=True)

def api_get(client: httpx.Client, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_BASE}/{endpoint}"
    # Empathy API seems to like these:
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": BASE,
        "referer": BASE + "/",
    }
    r = client.get(url, params=params, headers=headers)
    r.raise_for_status()
    return r.json()

def fetch_html(client: httpx.Client, url: str) -> str:
    r = client.get(url, headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    r.raise_for_status()
    return r.text

# --- parsing (HTML) ----------------------------------------------------------

def parse_breadcrumb(doc: HTMLParser) -> Tuple[List[str], str]:
    # Find ol that contains li[aria-label="home"]
    ol = None
    home = doc.css_first('li[aria-label="home"]')
    if home:
        node = home
        while node is not None:
            if node.tag == "ol":
                ol = node
                break
            node = node.parent
    if not ol:
        return [], ""
    parts: List[str] = []
    for a in ol.css("li a"):
        txt = clean_text(a.text())
        if txt:
            parts.append(txt)
    return parts, (parts[-1] if parts else "")

def _normalize_paragraphs(raw: str) -> str:
    raw = (raw or "").replace("\xa0", " ").strip()
    if not raw:
        return ""
    lines = [clean_text(l) for l in raw.splitlines()]
    paras: List[str] = []
    buf: List[str] = []
    for ln in lines:
        if ln:
            buf.append(ln)
        else:
            if buf:
                paras.append(" ".join(buf).strip())
                buf = []
    if buf:
        paras.append(" ".join(buf).strip())
    out = "\n".join([p for p in paras if p]).strip()
    # Si no había saltos reales, al menos devolvemos el texto limpio
    return out or clean_text(raw)


def parse_synopsis(doc: HTMLParser) -> str:
    """Extrae la sinopsis del HTML.

    Casa del Libro a veces renderiza la sinopsis como <p>...</p>, y otras veces
    como texto plano con saltos de línea dentro de `.resumen-content`.
    """
    # Preferimos el bloque cercano al h2 "Sinopsis de ..."
    for h2 in doc.css("h2"):
        t = clean_text(h2.text())
        if t.lower().startswith("sinopsis de"):
            nxt = h2.next
            for _ in range(40):
                if not nxt:
                    break
                if nxt.tag == "div" and ("resumen" in (nxt.attributes.get("class") or "")):
                    content = nxt.css_first(".resumen-content") or nxt
                    ps = [clean_text(p.text()) for p in content.css("p")]
                    ps = [p for p in ps if p]
                    if ps:
                        return "\n".join(ps).strip()
                    return _normalize_paragraphs(content.text())
                nxt = nxt.next

    # Fallback: elegimos el .resumen-content más largo
    best = ""
    for node in doc.css(".resumen-content"):
        txt = _normalize_paragraphs(node.text())
        if len(txt) > len(best):
            best = txt
    return best.strip()


def parse_title(doc: HTMLParser) -> str:
    t = doc.css_first("#t-p-f")
    if t:
        return clean_text(t.text())
    # fallback: h1
    h1 = doc.css_first("h1")
    return clean_text(h1.text()) if h1 else ""

def parse_ficha_tecnica(doc: HTMLParser) -> Dict[str, str]:
    ficha = doc.css_first("div.ficha-tecnica#contributors") or doc.css_first("div.ficha-tecnica")
    if not ficha:
        return {}
    out: Dict[str, str] = {}
    for node in ficha.css("[data-campo]"):
        key = clean_text(node.attributes.get("data-campo") or "")
        if not key:
            continue
        # Remove tooltip content (best-effort): if present, drop its text later by stripping duplicates.
        # Remove label <b> text by ignoring it when slicing.
        # With selectolax we can't easily decompose; we just post-process.
        full = clean_text(node.text())
        # Remove label prefix like "ISBN:" etc.
        full = re.sub(r"^[^:]{1,40}:\s*", "", full)
        full = dedupe_double(full)
        if key.lower() in ("serie/saga", "serie", "saga"):
            a = node.css_first("a")
            if a:
                out["Serie/Saga"] = clean_text(a.text())
                out["Serie/Saga URL"] = a.attributes.get("href") or ""
                continue
        out[key] = full
    return out

def pick_cover_from_html(doc: HTMLParser, isbn: str, title: str) -> str:
    isbn = (isbn or "").strip()
    title_l = (title or "").lower()
    candidates = doc.css("cdl-img img") + doc.css("img")
    best = ""
    best_score = -1
    for img in candidates:
        src = img.attributes.get("src") or ""
        srcset = img.attributes.get("srcset") or ""
        alt = (img.attributes.get("alt") or "").lower()
        blob = " ".join([src, srcset, alt]).lower()
        score = 0
        if isbn and isbn in blob:
            score += 10
        if title_l and title_l[:10] and title_l[:10] in blob:
            score += 3
        if "casadellibro.com" in blob:
            score += 1
        if score > best_score and (src or srcset):
            best_score = score
            best = best_img_from_srcset(img)
    return best

# --- API helpers -------------------------------------------------------------

def api_params_common() -> Dict[str, Any]:
    return {
        "internal": "true",
        "instance": "cdl",
        "lang": "es",
        "scope": "desktop",
        "currency": "EUR",
        "store": "ES",
    }

def api_search(client: httpx.Client, query: str, start: int = 0, rows: int = 24) -> List[Dict[str, Any]]:
    params = api_params_common()
    params.update({
        "query": query,
        "origin": "search_box:none",
        "start": start,
        "rows": rows,
        "facets": "false",
    })
    data = api_get(client, "search", params)
    return (data.get("catalog") or {}).get("content") or []

def api_isbnsearch(client: httpx.Client, query: str, start: int = 0, rows: int = 24) -> List[Dict[str, Any]]:
    params = api_params_common()
    params.update({
        "query": query,
        "origin": "search_box:none",
        "start": start,
        "rows": rows,
    })
    data = api_get(client, "isbnsearch", params)
    return (data.get("catalog") or {}).get("content") or []

def api_item_for_product_url(items: List[Dict[str, Any]], product_url: str) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    pu = (product_url or "").rstrip("/")
    # exact match on url
    for it in items:
        u = (it.get("url") or it.get("__url") or "").rstrip("/")
        if u == pu:
            return it
    # fallback: compare by id in url
    try:
        pid = pu.rstrip("/").split("/")[-1]
    except Exception:
        pid = ""
    if pid:
        for it in items:
            if str(it.get("id")) == pid or str(it.get("__id")) == pid:
                return it
    return items[0]

# --- main product ------------------------------------------------------------

def build_product(url: str) -> Dict[str, Any]:
    with make_client() as client:
        html = fetch_html(client, url)
        doc = HTMLParser(html)

        titulo = parse_title(doc)
        categoria_path, categoria = parse_breadcrumb(doc)
        descripcion = parse_synopsis(doc)
        ficha = parse_ficha_tecnica(doc)

        isbn_url = extract_isbn_from_url(url)
        isbn = ficha.get("ISBN") or isbn_url
        isbn = clean_text(isbn)

        # API enrichment (author/price/cover)
        raw_api = None
        autor = ""
        precio = None
        moneda = "EUR"
        encuadernacion = ficha.get("Encuadernación") or ""
        editorial = ficha.get("Editorial") or ""

        if isbn:
            api_items = api_isbnsearch(client, isbn, start=0, rows=24)
            raw_api = api_item_for_product_url(api_items, url)
            if raw_api:
                autor = clean_text(raw_api.get("author1") or (raw_api.get("authors") or [""])[0] or "")
                precio = raw_api.get("priceOffer")
                if precio is None and isinstance(raw_api.get("price"), dict):
                    precio = raw_api["price"].get("current")
                # editorial & encuadernacion fallbacks
                editorial = editorial or clean_text(raw_api.get("editorial") or "")
                encuadernacion = encuadernacion or clean_text(raw_api.get("encuadernation") or "")

        # Cover: prefer API __images[0], otherwise HTML-scored
        url_portada = ""
        if raw_api and raw_api.get("__images"):
            try:
                url_portada = raw_api["__images"][0]
            except Exception:
                url_portada = ""
        if not url_portada:
            url_portada = pick_cover_from_html(doc, isbn, titulo)

        # Other fields from ficha
        idioma = ficha.get("Idioma") or ""
        paginas = ficha.get("Número de páginas") or ""
        peso = ficha.get("Peso") or ""

        alto = ficha.get("Alto") or ""
        ancho = ficha.get("Ancho") or ""
        grueso = ficha.get("Grueso") or ""
        dimensiones = ""
        if alto and ancho and grueso:
            dimensiones = f"{alto} x {ancho} x {grueso}"

        fecha_publicacion = ficha.get("Fecha de lanzamiento") or ""
        # Some books show yearPublication only in API; keep html first.
        if not fecha_publicacion and raw_api:
            # raw_api['dateRelease'] is a timestamp; we won't format without timezone certainty.
            pass

        # If author missing, try HTML near title as last resort (but avoid "grid" contamination)
        if not autor:
            # Look for first author link within a small DOM neighborhood of title element
            tnode = doc.css_first("#t-p-f")
            if tnode:
                scope = tnode.parent
                for _ in range(5):
                    if not scope:
                        break
                    a = scope.css_first('a[href^="/libros-ebooks/"]')
                    if a:
                        autor = clean_text(a.text())
                        break
                    scope = scope.parent

        out: Dict[str, Any] = {
            "site": SITE,
            "url": url,
            "titulo": titulo,
            "autor": autor,
            "editorial": editorial,
            "isbn": isbn,
            "idioma": idioma,
            "paginas": paginas,
            "dimensiones": dimensiones,
            "peso": peso,
            "fecha_publicacion": fecha_publicacion,
            "categoria": categoria,
            "descripcion": clean_text(descripcion.replace("\n", " ")),
            "url_portada": url_portada,
            "precio": precio,
            "moneda": moneda,
            "encuadernacion": encuadernacion,
            "categoria_path": categoria_path,
            "info_adicional": ficha,
            "raw_api": raw_api,
        }
        return out

# --- cli ---------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> int:
    q = args.q
    rows = int(args.rows)
    with make_client() as client:
        if is_isbn_query(q):
            items = api_isbnsearch(client, re.sub(r"[^0-9Xx]", "", q), rows=rows)
        else:
            items = api_search(client, q, rows=rows)
    for it in items[:rows]:
        u = it.get("url") or it.get("__url")
        if u:
            print(u)
    return 0

def cmd_product(args: argparse.Namespace) -> int:
    data = build_product(args.url)
    print(data)
    return 0

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="casa_del_libro_experimental.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("search", help="Search Casa del Libro by ISBN or title")
    p1.add_argument("--q", required=True, help="ISBN or title")
    p1.add_argument("--rows", default="24", help="Max results (default 24)")
    p1.set_defaults(func=cmd_search)

    p2 = sub.add_parser("product", help="Parse product page and enrich via API")
    p2.add_argument("--url", required=True, help="Product URL")
    p2.set_defaults(func=cmd_product)

    args = parser.parse_args(argv)
    return int(args.func(args))

if __name__ == "__main__":
    raise SystemExit(main())
