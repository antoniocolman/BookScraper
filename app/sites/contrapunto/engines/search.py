#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Contrapunto (Shopify) - Engine search/detail (ported from experimental)

Qué mejora vs. versión anterior:
- Intenta primero el endpoint público Shopify `/products/<handle>.js` (si está habilitado):
  - suele incluir: id, handle, tags, vendor, product_type, variantes (precio, disponibilidad, sku)
  - es más estable que parsear HTML (menos frágil a cambios de theme).
- Fallback a HTML + JSON-LD si el `.js` no está disponible.
- Extrae mejor: autores múltiples, badges ("Oferta", "Novedad"), precio normal vs. tachado,
  IDs (product_id / variant_id) cuando aparecen en el HTML de tarjetas.
- CLI adicional: `search` para debug/POC (parsea grilla de resultados de Shopify).

Nota ética: úsalo con permiso y con pausas (`--sleep`) para no saturar el sitio.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qsl

import requests
from bs4 import BeautifulSoup


# Import compartido (normalización) sin romper ejecución directa por archivo.
try:
    from app.standard import normalize_encuadernacion
except Exception:  # pragma: no cover
    from pathlib import Path

    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "app" / "standard.py").exists():
            sys.path.insert(0, str(p))
            break

    try:
        from app.standard import normalize_encuadernacion
    except Exception:
        # Fallback minimal para ejecución aislada
        def normalize_encuadernacion(value):  # type: ignore
            v = str(value or "").strip()
            if not v:
                return ""
            low = v.lower()
            if "tapa dura" in low or "hardcover" in low or "hardback" in low:
                return "Tapa Dura"
            if "rustica" in low or "rústica" in low or "tapa blanda" in low or "softcover" in low:
                return "Rústica"
            if "paperback" in low or "bolsillo" in low:
                return "De Bolsillo"
            return v


BASE_URL = "https://contrapunto.cl"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}


# -----------------------------
# Helpers
# -----------------------------
def sleep_delay(base: float) -> None:
    """Sleep with a small jitter, to be nicer to the server."""
    if base <= 0:
        return
    jitter = random.uniform(0.15, 0.65)
    time.sleep(base + jitter)


def canonicalize_url(url: str, base: str = BASE_URL) -> str:
    parsed = urlparse(url)
    clean = parsed._replace(query="", fragment="")
    if clean.scheme == "":
        if url.startswith("//"):
            return "https:" + url
        return urljoin(base, url)
    return urlunparse(clean)


def canonicalize_product_url(url: str) -> str:
    return canonicalize_url(url, BASE_URL)


def strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).strip()


def normalize_isbn(s: str) -> str:
    s = re.sub(r"[^0-9Xx]", "", (s or "").strip())
    return s.upper()


def looks_like_isbn(s: str) -> bool:
    s = normalize_isbn(s)
    return len(s) in (10, 13) and bool(re.match(r"^[0-9X]{10}$|^[0-9]{13}$", s))


def clean_price_to_int(price_str: str) -> Optional[int]:
    """Extract integer CLP-like amount from strings like '$76.330'."""
    if not price_str:
        return None
    m = re.search(r"([0-9][0-9\.\,]*)", price_str)
    if not m:
        return None
    raw = m.group(1).strip().replace(".", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None


def normalize_shopify_minor_units(value: Optional[int]) -> Optional[int]:
    """
    Shopify (/products/<handle>.js y endpoints similares) suelen entregar precios en *minor units*
    (p.ej. "cents"): $9.265 CLP suele venir como 926500.

    Para Contrapunto (CLP), normalizamos a unidades "humanas" (int) dividiendo por 100 cuando el
    número claramente parece venir en minor units.

    Heurística segura para CLP:
    - Si es >= 100.000 y es múltiplo de 100, asumimos minor units y dividimos por 100.
      (Evita romper casos raros donde ya venga en unidades, como 10900).
    """
    if value is None:
        return None

    # minor-units típico en Shopify: múltiplo de 100
    if value >= 100_000 and value % 100 == 0:
        return value // 100

    return value


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def http_get(session: requests.Session, url: str, *, timeout: float = 30.0, retries: int = 3) -> requests.Response:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * attempt + random.uniform(0.0, 0.75))
                continue
            raise last_err  # type: ignore[misc]


def build_search_url(q: str, *, prefix_last: bool = True, base: str = BASE_URL) -> str:
    params = [("q", q)]
    if prefix_last:
        params.append(("options[prefix]", "last"))
    return f"{base}/search?{urlencode(params)}"


def product_js_url(product_url: str) -> Optional[str]:
    """
    Convierte:
      https://contrapunto.cl/products/handle?... -> https://contrapunto.cl/products/handle.js
    """
    u = urlparse(product_url)
    path = u.path.rstrip("/")
    if not path.startswith("/products/"):
        return None
    if path.endswith(".js"):
        return urlunparse(u._replace(query="", fragment=""))
    js_path = path + ".js"
    return urlunparse(u._replace(path=js_path, query="", fragment=""))


# -----------------------------
# Data model
# -----------------------------
@dataclass
class ContraPuntoBook:
    site: str
    url: str

    # básicos
    titulo: Optional[str] = None
    autor: Optional[str] = None

    # ficha "libro"
    editorial: Optional[str] = None
    isbn: Optional[str] = None
    encuadernacion: Optional[str] = None
    idioma: Optional[str] = None
    paginas: Optional[int] = None
    dimensiones: Optional[str] = None
    sinopsis: Optional[str] = None
    fecha_publicacion: Optional[str] = None

    # media & precio
    url_portada: Optional[str] = None
    precio: Optional[int] = None
    precio_tachado: Optional[int] = None
    moneda: Optional[str] = "CLP"
    disponibilidad: Optional[str] = None

    # shopify-ish extras (si están disponibles)
    handle: Optional[str] = None
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    badges: List[str] = field(default_factory=list)

    # debug
    raw_json: Optional[dict] = None


# -----------------------------
# Listing / Search parsing
# -----------------------------
def parse_grid_cards(html: str, base_url: str = BASE_URL) -> List[dict]:
    """
    Parsea cards tipo:
      <ul id="product-grid"> ... <li class="grid__item"> ... </li> </ul>

    Devuelve hits con:
      url, titulo, autores, badges, precio_sale, precio_regular, precio_tachado, product_id, variant_id, img
    """
    soup = BeautifulSoup(html, "html.parser")
    hits: List[dict] = []

    for li in soup.select("#product-grid li.grid__item"):
        a = li.select_one('a[href^="/products/"]')
        if not a:
            continue

        url = canonicalize_url(a.get("href", "").split("?")[0], base=base_url)
        titulo = a.get_text(strip=True)

        # autores (en tarjetas suele venir en caption-with-letter-spacing)
        author_el = li.select_one(".card-information .caption-with-letter-spacing")
        autores = author_el.get_text(" ", strip=True) if author_el else None

        # badges visibles en la tarjeta
        badges = [b.get_text(strip=True) for b in li.select(".card__badge .badge") if b.get_text(strip=True)]
        badges = sorted(set(badges))

        # precios (sale / regular / tachado)
        sale_el = li.select_one(".price-item--sale")
        reg_el = li.select_one(".price__regular .price-item--regular")
        compare_el = li.select_one(".price__sale s.price-item--regular")

        precio_sale = clean_price_to_int(sale_el.get_text(strip=True)) if sale_el else None
        precio_regular = clean_price_to_int(reg_el.get_text(strip=True)) if reg_el else None
        precio_tachado = clean_price_to_int(compare_el.get_text(strip=True)) if compare_el else None

        # ids
        variant_el = li.select_one("input.product-variant-id")
        variant_id = variant_el.get("value") if variant_el else None

        product_id_el = li.select_one('input[name="product-id"]')
        product_id = product_id_el.get("value") if product_id_el else None

        # imagen
        img = li.select_one(".card__media img")
        img_src = img.get("src") if img else None
        if img_src and img_src.startswith("//"):
            img_src = "https:" + img_src

        hits.append(
            {
                "url": url,
                "titulo": titulo,
                "autores": autores,
                "badges": badges,
                "precio_sale": precio_sale,
                "precio_regular": precio_regular,
                "precio_tachado": precio_tachado,
                "product_id": product_id,
                "variant_id": variant_id,
                "img": img_src,
            }
        )

    return hits


def extract_product_urls_from_listing(html: str) -> List[str]:
    """Compat: versión vieja retornaba solo URLs."""
    hits = parse_grid_cards(html)
    if hits:
        return sorted({h["url"] for h in hits if h.get("url")})
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a in soup.select('a[href*="/products/"]'):
        href = a.get("href")
        if href and "/products/" in href:
            urls.add(canonicalize_product_url(urljoin(BASE_URL, href)))
    return sorted(urls)


# -----------------------------
# Product parsing
# -----------------------------
def _parse_jsonld_product(soup: BeautifulSoup) -> Optional[dict]:
    """
    Intenta extraer el objeto Product/Book desde JSON-LD.
    Soporta:
      - dict directo
      - lista
      - @graph
    """
    for tag in soup.select('script[type="application/ld+json"]'):
        txt = (tag.string or "").strip()
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue

        candidates: List[dict] = []
        if isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates.extend([x for x in data["@graph"] if isinstance(x, dict)])
            else:
                candidates.append(data)
        elif isinstance(data, list):
            candidates.extend([x for x in data if isinstance(x, dict)])

        for item in candidates:
            t = item.get("@type")
            if isinstance(t, list):
                ok = any(x in ("Product", "Book") for x in t)
            else:
                ok = t in ("Product", "Book")
            if ok:
                return item
    return None


def _extract_from_metadata_block(soup: BeautifulSoup) -> Dict[str, str]:
    """
    Contrapunto parece imprimir una ficha tipo 'pivot-metadata' con pares KEY: VALUE.
    Esto intenta ser tolerante a variaciones de formato.
    """
    meta: Dict[str, str] = {}

    pivot = soup.select_one("p.pivot-metadata")
    if not pivot:
        return meta

    # strings ya "flattened"
    tokens = [t.strip() for t in pivot.stripped_strings if t.strip()]

    # Caso A: tokens ["EDITORIAL:", "Blume", "ISBN:", "978...", ...]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.endswith(":"):
            key = tok[:-1].strip()
            val = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
            meta[key] = val
            i += 2
            continue

        # Caso B: token "EDITORIAL: Blume"
        if ":" in tok:
            k, v = tok.split(":", 1)
            meta[k.strip()] = v.strip()
        i += 1

    return meta


def _apply_book_fields_from_meta(book: ContraPuntoBook, meta: Dict[str, str]) -> None:
    norm = {strip_accents(k).upper(): v for k, v in meta.items()}

    book.editorial = book.editorial or norm.get("EDITORIAL")
    isbn = norm.get("ISBN") or norm.get("ISBN-13") or norm.get("ISBN13")
    if isbn and not book.isbn:
        book.isbn = normalize_isbn(isbn)

    enc = norm.get("ENCUADERNACION") or norm.get("ENCUADERNACIÓN")
    if enc and not book.encuadernacion:
        book.encuadernacion = normalize_encuadernacion(enc)

    book.idioma = book.idioma or norm.get("IDIOMA")
    book.dimensiones = book.dimensiones or norm.get("DIMENSIONES")

    paginas = norm.get("N° DE PAGINAS") or norm.get("N DE PAGINAS") or norm.get("Nº DE PAGINAS") or norm.get("PAGINAS")
    if paginas and book.paginas is None:
        m = re.search(r"\d+", paginas.replace(".", ""))
        if m:
            try:
                book.paginas = int(m.group(0))
            except Exception:
                pass

    # fecha publicación (si existe)
    fp = norm.get("FECHA DE PUBLICACION") or norm.get("FECHA DE PUBLICACIÓN") or norm.get("PUBLICACION") or norm.get("PUBLICACIÓN")
    if fp and not book.fecha_publicacion:
        book.fecha_publicacion = _clean_text(fp)


def _extract_authors(soup: BeautifulSoup) -> Optional[str]:
    # Producto: suelen venir como links dentro de .pivot-authors
    auts = [a.get_text(" ", strip=True) for a in soup.select(".pivot-authors a") if a.get_text(strip=True)]
    auts = [a for a in auts if a]
    if auts:
        return ", ".join(dict.fromkeys(auts))  # de-dup conservando orden

    # fallback (tarjetas/listados)
    cap = soup.select_one(".card-information .caption-with-letter-spacing")
    if cap:
        txt = cap.get_text(" ", strip=True)
        return txt or None

    return None


def _extract_cover(soup: BeautifulSoup) -> Optional[str]:
    img = soup.select_one(".product__media img") or soup.select_one('meta[property="og:image"]')
    if not img:
        return None
    if img.name == "meta":
        src = img.get("content")
    else:
        src = img.get("src") or img.get("data-src")
    if not src:
        return None
    if src.startswith("//"):
        src = "https:" + src
    return src


def _extract_badges(soup: BeautifulSoup) -> List[str]:
    badges = [b.get_text(strip=True) for b in soup.select(".badge") if b.get_text(strip=True)]
    return sorted(set(badges))


def _extract_price_from_html(soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    """
    Retorna (precio, precio_tachado)
    """
    sale_el = soup.select_one(".price-item--sale")
    reg_el = soup.select_one(".price__regular .price-item--regular")
    compare_el = soup.select_one(".price__sale s.price-item--regular")

    precio = clean_price_to_int(sale_el.get_text(strip=True)) if sale_el else None
    if precio is None and reg_el:
        precio = clean_price_to_int(reg_el.get_text(strip=True))

    precio_tachado = clean_price_to_int(compare_el.get_text(strip=True)) if compare_el else None
    return precio, precio_tachado


def _extract_sinopsis(soup: BeautifulSoup) -> Optional[str]:
    """
    Shopify (tema Dawn y derivados) suele mostrar la sinopsis/descripcion dentro de
    acordeones <details> con <summary>. Elegimos el bloque cuyo summary tenga
    señales como 'Descripción', 'Sinopsis', etc.

    Nota: en páginas con varios acordeones (envíos, devoluciones, etc.) el primer
    `.accordion__content` no siempre es la sinopsis, por eso filtramos por el título.
    """
    keywords = [
        "descripcion",
        "sinopsis",
        "reseña",
        "resena",
        "resumen",
        "contraportada",
        "contenido",
    ]

    def _matches_summary(txt: str) -> bool:
        t = strip_accents(txt.lower())
        return any(k in t for k in [strip_accents(k) for k in keywords])

    # 1) Prefer: acordeón cuyo summary coincide con keywords
    for details in soup.select("details"):
        summary = details.select_one("summary")
        if not summary:
            continue
        title = summary.get_text(" ", strip=True)
        if not title:
            continue
        if _matches_summary(title):
            body = details.select_one(".accordion__content.rte") or details.select_one(".accordion__content")
            if body:
                txt = body.get_text("\n", strip=True)
                if txt:
                    return _clean_text(txt)

    # 2) Fallback: primer bloque accordion__content.rte que tenga texto decente
    for syn in soup.select("div.accordion__content.rte, div.accordion__content"):
        txt = syn.get_text("\n", strip=True)
        if txt and len(txt) > 80:  # heurística: evita bloques vacíos/cortos
            return _clean_text(txt)

    # 3) Fallback: meta tags (a veces vienen con resumen)
    meta = soup.select_one('meta[property="og:description"]') or soup.select_one('meta[name="description"]')
    if meta:
        content = meta.get("content", "")
        if isinstance(content, str) and content.strip():
            return _clean_text(content)

    return None


def _apply_from_jsonld(book: ContraPuntoBook, data: dict) -> None:
    book.raw_json = book.raw_json or data

    # title
    if not book.titulo:
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            book.titulo = _clean_text(name)

    # description
    if not book.sinopsis:
        desc = data.get("description")
        if isinstance(desc, str) and desc.strip():
            book.sinopsis = _clean_text(desc)

    # isbn/gtin
    if not book.isbn:
        for k in ("gtin13", "isbn", "sku"):
            v = data.get(k)
            if isinstance(v, str) and looks_like_isbn(v):
                book.isbn = normalize_isbn(v)
                break

    # offers/price
    if book.precio is None:
        offers = data.get("offers")
        if isinstance(offers, dict):
            price = offers.get("price")
            if isinstance(price, (int, float)):
                book.precio = int(price)
            elif isinstance(price, str):
                book.precio = clean_price_to_int(price)
        elif isinstance(offers, list) and offers:
            # elige primera oferta
            off0 = offers[0]
            if isinstance(off0, dict):
                price = off0.get("price")
                if isinstance(price, (int, float)):
                    book.precio = int(price)
                elif isinstance(price, str):
                    book.precio = clean_price_to_int(price)

    # availability
    if not book.disponibilidad:
        offers = data.get("offers")
        if isinstance(offers, dict):
            av = offers.get("availability")
            if isinstance(av, str) and "InStock" in av:
                book.disponibilidad = "disponible"
            elif isinstance(av, str) and "OutOfStock" in av:
                book.disponibilidad = "agotado"


def _apply_from_product_js(book: ContraPuntoBook, data: dict) -> None:
    """
    Shopify /products/<handle>.js
    """
    book.raw_json = book.raw_json or data

    # basic ids
    if data.get("id") is not None and not book.product_id:
        book.product_id = str(data.get("id"))
    if isinstance(data.get("handle"), str) and not book.handle:
        book.handle = data["handle"]

    # text fields
    if isinstance(data.get("title"), str) and not book.titulo:
        book.titulo = _clean_text(data["title"])
    if isinstance(data.get("vendor"), str) and not book.vendor:
        book.vendor = _clean_text(data["vendor"])
    if isinstance(data.get("product_type"), str) and not book.product_type:
        book.product_type = _clean_text(data["product_type"])

    # description (HTML) -> sinopsis
    if not book.sinopsis:
        desc = data.get("description")
        if isinstance(desc, str) and desc.strip():
            try:
                # /products/<handle>.js suele traer HTML; lo convertimos a texto
                soup_desc = BeautifulSoup(desc, "html.parser")
                txt = soup_desc.get_text("\n", strip=True)
                book.sinopsis = _clean_text(txt if txt else desc)
            except Exception:
                book.sinopsis = _clean_text(desc)

    # tags (Shopify suele devolver string con coma)
    tags = data.get("tags")
    if tags and not book.tags:
        if isinstance(tags, str):
            book.tags = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            book.tags = [str(t).strip() for t in tags if str(t).strip()]

    # images
    if not book.url_portada:
        images = data.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str) and first:
                book.url_portada = canonicalize_url(first)

    # variants -> precio / compare_at / availability / sku as isbn
    variants = data.get("variants")
    if isinstance(variants, list) and variants:
        # elige: primera variante disponible, si no, la primera
        chosen = None
        for v in variants:
            if isinstance(v, dict) and v.get("available") is True:
                chosen = v
                break
        if chosen is None:
            chosen = variants[0] if isinstance(variants[0], dict) else None

        if isinstance(chosen, dict):
            if chosen.get("id") is not None and not book.variant_id:
                book.variant_id = str(chosen.get("id"))

            # precio (puede venir en minor units)
            p = chosen.get("price")
            if isinstance(p, str) and p.isdigit():
                p_int = int(p)
            elif isinstance(p, int):
                p_int = p
            else:
                p_int = None

            p_cmp = chosen.get("compare_at_price")
            if isinstance(p_cmp, str) and p_cmp.isdigit():
                p_cmp_int = int(p_cmp)
            elif isinstance(p_cmp, int):
                p_cmp_int = p_cmp
            else:
                p_cmp_int = None

            if book.precio is None and p_int is not None:
                book.precio = normalize_shopify_minor_units(p_int)
            if book.precio_tachado is None and p_cmp_int is not None:
                book.precio_tachado = normalize_shopify_minor_units(p_cmp_int)

            # sku -> isbn
            if not book.isbn:
                sku = chosen.get("sku")
                if isinstance(sku, str) and looks_like_isbn(sku):
                    book.isbn = normalize_isbn(sku)

        # disponibilidad global
        if not book.disponibilidad:
            any_avail = any(isinstance(v, dict) and v.get("available") is True for v in variants)
            book.disponibilidad = "disponible" if any_avail else "agotado"


def parse_product_page_html(html: str, url: str) -> ContraPuntoBook:
    soup = BeautifulSoup(html, "html.parser")
    book = ContraPuntoBook(site="contrapunto", url=canonicalize_product_url(url))

    # JSON-LD first (si existe)
    jsonld = _parse_jsonld_product(soup)
    if jsonld:
        _apply_from_jsonld(book, jsonld)

    # Título
    h1 = soup.select_one(".product__title h1") or soup.select_one("h1")
    if h1 and not book.titulo:
        book.titulo = h1.get_text(strip=True)

    # Autor(es)
    if not book.autor:
        book.autor = _extract_authors(soup)

    # Portada
    if not book.url_portada:
        book.url_portada = _extract_cover(soup)

    # Badges (si aparecen)
    book.badges = book.badges or _extract_badges(soup)

    # Metadata pivot
    meta = _extract_from_metadata_block(soup)
    if meta:
        _apply_book_fields_from_meta(book, meta)

    # Sinopsis
    if not book.sinopsis:
        book.sinopsis = _extract_sinopsis(soup)

    # Precio (HTML)
    if book.precio is None or book.precio_tachado is None:
        p, p_cmp = _extract_price_from_html(soup)
        if book.precio is None:
            book.precio = p
        if book.precio_tachado is None:
            book.precio_tachado = p_cmp

    # Disponibilidad (fallback por texto)
    if not book.disponibilidad:
        text_all = soup.get_text("\n", strip=True)
        if re.search(r"\bAgotado\b", text_all, flags=re.IGNORECASE):
            book.disponibilidad = "agotado"
        elif re.search(r"\bÚltimas unidades\b", text_all, flags=re.IGNORECASE):
            book.disponibilidad = "ultimas_unidades"
        elif re.search(r"\bDisponible\b", text_all, flags=re.IGNORECASE):
            book.disponibilidad = "disponible"

    if book.url_portada:
        book.url_portada = canonicalize_url(book.url_portada)

    return book


def parse_product(session: requests.Session, url: str, *, prefer_js: bool = True, timeout: float = 30.0, retries: int = 3, sleep: float = 0.0) -> ContraPuntoBook:
    url = canonicalize_product_url(url)

    # 1) intentamos product.js (si está habilitado)
    if prefer_js:
        js = product_js_url(url)
        if js:
            try:
                r = http_get(session, js, timeout=timeout, retries=retries)
                # Shopify devuelve JS tipo "var meta = ..."? En product.js típicamente es JSON puro.
                data = r.json()
                book = ContraPuntoBook(site="contrapunto", url=url)
                _apply_from_product_js(book, data)

                # si el JSON no trae sinopsis/editorial/idioma/etc, complementamos con HTML (una sola visita)
                needs_html = any(
                    [
                        not book.sinopsis,
                        not book.editorial,
                        not book.encuadernacion,
                        not book.idioma,
                        book.paginas is None,
                        not book.dimensiones,
                    ]
                )
                if needs_html:
                    sleep_delay(sleep)
                    r2 = http_get(session, url, timeout=timeout, retries=retries)
                    book_html = parse_product_page_html(r2.text, url)

                    # merge: preferimos lo ya obtenido por JS para ids/tags/precio; completamos lo faltante
                    for k, v in asdict(book_html).items():
                        if k == "tags":
                            if not book.tags and v:
                                book.tags = v
                            continue
                        if k == "badges":
                            if not book.badges and v:
                                book.badges = v
                            continue
                        if getattr(book, k, None) in (None, "", []) and v not in (None, "", []):
                            setattr(book, k, v)
                    return book

                return book
            except Exception:
                # si falla, caemos a HTML
                pass

    # 2) Fallback HTML
    sleep_delay(sleep)
    r = http_get(session, url, timeout=timeout, retries=retries)
    return parse_product_page_html(r.text, url)


# -----------------------------
# CLI commands
# -----------------------------
def cmd_product(args: argparse.Namespace) -> int:
    s = get_session()
    book = parse_product(
        s,
        args.url,
        prefer_js=not args.no_js,
        timeout=args.timeout,
        retries=args.retries,
        sleep=args.sleep,
    )
    print(json.dumps(asdict(book), ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    s = get_session()
    url = build_search_url(args.q, prefix_last=not args.no_prefix_last, base=BASE_URL)
    r = http_get(s, url, timeout=args.timeout, retries=args.retries)
    hits = parse_grid_cards(r.text, base_url=BASE_URL)

    # opcional: sólo urls (para compat con flujos antiguos)
    if args.only_urls:
        out = sorted({h["url"] for h in hits if h.get("url")})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # opcional: resolver cada hit visitando la página del producto (para obtener sinopsis, ISBN, etc.)
    if args.resolve:
        resolved = []
        for h in hits[: args.max_hits]:
            u = h.get("url")
            if not u:
                continue

            book = parse_product(
                s,
                u,
                prefer_js=not args.no_js,
                timeout=args.timeout,
                retries=args.retries,
                sleep=args.sleep,
            )

            # merge: preferimos valores del producto; completamos con lo del listado si falta
            if not book.autor and h.get("autores"):
                book.autor = h["autores"]
            if not book.url_portada and h.get("img"):
                book.url_portada = h["img"]
            if not book.product_id and h.get("product_id"):
                book.product_id = str(h["product_id"])
            if not book.variant_id and h.get("variant_id"):
                book.variant_id = str(h["variant_id"])

            # precios: si la ficha no trae, usamos el del listado
            if book.precio is None and h.get("precio_sale") is not None:
                book.precio = int(h["precio_sale"])
            if book.precio_tachado is None and h.get("precio_tachado") is not None:
                book.precio_tachado = int(h["precio_tachado"])

            # badges: unión (listado suele traer Agotado/Novedad/Oferta)
            badges = set(book.badges or [])
            for b in h.get("badges") or []:
                badges.add(b)
            book.badges = sorted(badges)

            resolved.append(asdict(book))

        print(json.dumps(resolved, ensure_ascii=False, indent=2))
        return 0

    # default: output del listado
    print(json.dumps(hits, ensure_ascii=False, indent=2))
    return 0




def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("product", help="Parsear un producto directo (URL /products/...)")
    pp.add_argument("--url", required=True)
    pp.add_argument("--no-js", action="store_true", help="No intentar endpoint /products/<handle>.js")
    pp.add_argument("--timeout", type=float, default=30.0)
    pp.add_argument("--retries", type=int, default=3)
    pp.add_argument("--sleep", type=float, default=0.0, help="Pausa base (segundos) entre requests")
    pp.set_defaults(func=cmd_product)

    ps = sub.add_parser("search", help="Parsear resultados de búsqueda (grilla #product-grid)")
    ps.add_argument("--q", required=True, help="texto o ISBN")
    ps.add_argument("--no-prefix-last", action="store_true", help="No enviar options[prefix]=last")
    ps.add_argument("--only-urls", "--only-url", action="store_true", help="Imprimir solo URLs de productos")
    ps.add_argument("--resolve", action="store_true", help="Resolver cada resultado visitando el producto (incluye sinopsis, ISBN, tags, etc.)")
    ps.add_argument("--max-hits", type=int, default=5, help="Máximo de resultados a resolver (con --resolve)")
    ps.add_argument("--no-js", action="store_true", help="No intentar endpoint /products/<handle>.js al resolver")
    ps.add_argument("--sleep", type=float, default=0.0, help="Pausa base (segundos) entre requests al resolver")
    ps.add_argument("--timeout", type=float, default=30.0)
    ps.add_argument("--retries", type=int, default=3)
    ps.set_defaults(func=cmd_search)

    return p

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BASE_URL",
    "DEFAULT_HEADERS",
    "ContraPuntoBook",
    "get_session",
    "build_search_url",
    "parse_grid_cards",
    "extract_product_urls_from_listing",
    "parse_product",
    "parse_product_page_html",
]
