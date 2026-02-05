from __future__ import annotations

import json
import random
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.yenny-elateneo.com"

# User-Agent "realista" ayuda bastante con 403/429 en algunos hosts.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


def normalize_url(href: str, base_url: str = BASE_URL) -> str:
    """Normaliza href relativos y protocolless //..."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base_url, href)


def make_session(headers: Optional[Dict[str, str]] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    if headers:
        s.headers.update(headers)
    return s


def _extract_html_from_json_payload(payload: object) -> Optional[str]:
    """Yenny (MiTiendaNube) a veces responde JSON cuando se usa results_only=true."""
    if isinstance(payload, dict):
        # patrón común: {"html": "<div>...</div>", ...}
        html = payload.get("html")
        if isinstance(html, str) and html.strip():
            return html
        # a veces: {"data":{"html":"..."}}
        data = payload.get("data")
        if isinstance(data, dict):
            html = data.get("html")
            if isinstance(html, str) and html.strip():
                return html
        # a veces: {"results":[{"html":"..."}, ...]}
        results = payload.get("results")
        if isinstance(results, list):
            parts: List[str] = []
            for it in results:
                if isinstance(it, dict):
                    h = it.get("html") or it.get("content")
                    if isinstance(h, str) and h.strip():
                        parts.append(h)
                elif isinstance(it, str) and it.strip():
                    parts.append(it)
            if parts:
                return "\n".join(parts)
    return None


def fetch_html(session: requests.Session, url: str, timeout: float = 30.0, **kwargs) -> str:
    """
    GET y devuelve HTML.
    Si la respuesta es JSON (results_only), intenta extraer el HTML embebido.
    kwargs se ignora (sirve para compatibilidad con fetch_html(url, results_only=...)).
    """
    resp = session.get(url, timeout=timeout)
    # Para diagnósticos: dejá que el caller haga raise_for_status cuando corresponda
    resp.raise_for_status()

    ctype = (resp.headers.get("Content-Type") or "").lower()
    text = resp.text or ""

    if "application/json" in ctype or text.lstrip().startswith("{") or text.lstrip().startswith("["):
        try:
            payload = resp.json()
        except Exception:
            payload = None
        html = _extract_html_from_json_payload(payload) if payload is not None else None
        if html is not None:
            return html

    return text


def fetch_html_with_backoff(
    session: requests.Session,
    url: str,
    max_retries: int = 3,
    base_wait: float = 8.0,
    timeout: float = 30.0,
    jitter: Tuple[float, float] = (0.0, 1.5),
    retry_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504),
) -> str:
    """
    Backoff exponencial con jitter.
    - Respeta Retry-After si viene en 429.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_html(session, url, timeout=timeout)
        except requests.HTTPError as e:
            last_err = e
            status = getattr(e.response, "status_code", None)
            if status not in retry_statuses or attempt >= max_retries:
                raise

            retry_after = None
            if e.response is not None:
                ra = e.response.headers.get("Retry-After")
                if ra:
                    try:
                        retry_after = float(ra)
                    except Exception:
                        retry_after = None

            exp = base_wait * (2 ** (attempt - 1))
            wait = retry_after if retry_after is not None else exp
            wait += random.uniform(*jitter)

            print(f"[WARN] {status} en {url} (intento {attempt}/{max_retries}). Esperando {wait:.1f}s...")
            time.sleep(wait)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt >= max_retries:
                raise
            wait = base_wait * (2 ** (attempt - 1)) + random.uniform(*jitter)
            print(f"[WARN] Error de red en {url} (intento {attempt}/{max_retries}). Esperando {wait:.1f}s...")
            time.sleep(wait)

    if last_err:
        raise last_err
    raise RuntimeError("fetch_html_with_backoff falló sin excepción explícita (inusual).")

def extract_product_urls_from_listing(html: str, base_url: str = BASE_URL) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if "/productos/" not in href:
            continue

        full = normalize_url(href, base_url=base_url).rstrip("/")

        # ❗ Ignorar el "directorio" que NO es producto
        if full == "https://www.yenny-elateneo.com/productos":
            continue

        if full and full not in urls:
            urls.append(full)

    return urls

def _parse_price(text: str) -> Optional[float]:
    # " $38.800,00 " -> 38800.0
    if not text:
        return None
    t = text.strip()
    # limpiar símbolos
    t = t.replace("$", "").replace("ARS", "").replace("USD", "").strip()
    # miles con . y decimal con ,
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        return None


def _best_src_from_srcset(srcset: str) -> Optional[str]:
    # devuelve la última URL (normalmente la mayor) del srcset
    if not srcset:
        return None
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    if not parts:
        return None
    last = parts[-1].split(" ")[0].strip()
    return last or None


def parse_product_page(html: str, url: str) -> Dict[str, object]:
    """
    Devuelve un dict con campos normalizados:
      title, author, price, currency, isbn,
      portada_url, image_url,
      formato, editorial, encuadernacion, idioma, paginas, dimensiones, fecha_publicacion, sku,
      descripcion_raw, sinopsis, raw_data
    """
    soup = BeautifulSoup(html, "html.parser")

    # ---- título / autor ----
    title_el = soup.select_one("h1.js-product-name, h1[data-store^='product-name']")
    title = title_el.get_text(" ", strip=True) if title_el else None

    author_el = soup.select_one("p.text-accent.mb-3, a[href*='/search/?q='] p.text-accent")
    author = author_el.get_text(" ", strip=True) if author_el else None

    # ---- precio ----
    currency = "ARS"
    price = None

    price_el = soup.select_one("#price_display.js-price-display, #price_display, .js-price-display.h3")
    if price_el:
        data_price = price_el.get("data-product-price")
        if data_price and str(data_price).isdigit():
            price = float(int(str(data_price))) / 100.0
        else:
            price = _parse_price(price_el.get_text(" ", strip=True))

    # ---- portada / imagen ----
    portada_url = None
    image_url = None

    # 1) link del fancybox/galería (suele ser la imagen grande)
    a_cover = soup.select_one("a.js-product-slide-link[data-fancybox='product-gallery'], a[data-fancybox='product-gallery']")
    if a_cover and a_cover.get("href"):
        portada_url = normalize_url(a_cover.get("href"))

    # 2) imagen con srcset
    img_cover = soup.select_one("img.js-product-slide-img[data-srcset], img.js-product-slide-img[srcset], .swiper-wrapper img[srcset], .swiper-wrapper img[data-srcset]")
    if img_cover:
        srcset = img_cover.get("data-srcset") or img_cover.get("srcset") or ""
        best = _best_src_from_srcset(srcset)
        if best:
            image_url = normalize_url(best)
        else:
            src = img_cover.get("data-src") or img_cover.get("src")
            if src:
                image_url = normalize_url(src)

    if not portada_url and image_url:
        portada_url = image_url

    # ---- bloque descripción / técnicos / sinopsis ----
    desc_container = soup.select_one(".user-content, .user-content.font-small, .col-md-10.pr-md-5 .user-content")
    descripcion_raw = None
    sinopsis = None
    raw_data: Dict[str, str] = {}

    formato = editorial = encuadernacion = idioma = isbn = dimensiones = fecha_publicacion = sku = None
    paginas: Optional[int] = None

    if desc_container:
        descripcion_raw = str(desc_container)

        # Extraemos "líneas técnicas" del bloque std/short-description
        std = desc_container.select_one(".short-description .std") or desc_container.select_one(".std") or desc_container

        # La estructura suele ser: <div class="std"><strong>Label:</strong> valor</div>
        for block in std.find_all(["div", "p"], recursive=True):
            strong = block.find("strong")
            if not strong:
                continue
            label = strong.get_text(" ", strip=True)
            # si es "Sinópsis" no es un key-value
            if re.search(r"sin[oó]psis", label, re.IGNORECASE):
                continue

            # valor = texto del bloque sin el label
            value = block.get_text(" ", strip=True)
            value = value.replace(label, "", 1).strip()
            label_norm = re.sub(r"\s+", " ", label).strip().strip(":").lower()

            if value:
                raw_data[label_norm] = value

        # normalizamos llaves conocidas (si están)
        def pick(*keys: str) -> Optional[str]:
            for k in keys:
                if k in raw_data and raw_data[k]:
                    return raw_data[k]
            return None

        formato = pick("formato")
        editorial = pick("editorial")
        encuadernacion = pick("encuadernación", "encuadernacion")
        idioma = pick("idioma")
        isbn = pick("isbn")
        dimensiones = pick("dimensiones")
        fecha_publicacion = pick("fecha publicación", "fecha publicacion")

        pag = pick("n° páginas", "nº páginas", "n paginas", "páginas", "paginas", "n° paginas", "nº paginas")
        if pag:
            m = re.search(r"(\d+)", pag.replace(".", ""))
            if m:
                try:
                    paginas = int(m.group(1))
                except Exception:
                    paginas = None

        sku = pick("sku")

        # ---- sinopsis ----
        # buscamos el nodo "Sinópsis" y tomamos todo el texto posterior (p)
        syn_node = None
        for strong in desc_container.find_all("strong"):
            if re.search(r"sin[oó]psis", strong.get_text(" ", strip=True), re.IGNORECASE):
                syn_node = strong
                break

        if syn_node:
            p = syn_node.find_parent("p")
            texts: List[str] = []
            if p:
                # texto del mismo <p> sin el "Sinópsis"
                t = p.get_text(" ", strip=True)
                t = re.sub(r"(?i)sin[oó]psis", "", t).strip(" :—-")
                if t:
                    texts.append(t)
                # siguientes <p>
                sib = p.find_next_sibling()
                while sib:
                    if getattr(sib, "name", None) == "p":
                        t2 = sib.get_text(" ", strip=True)
                        if t2:
                            texts.append(t2)
                    sib = sib.find_next_sibling()
            if texts:
                sinopsis = "\n".join(texts).strip()

    return {
        "url": url,
        "title": title,
        "author": author,
        "price": price,
        "currency": currency,
        "isbn": isbn,
        "portada_url": portada_url,
        "image_url": image_url,
        "formato": formato,
        "editorial": editorial,
        "encuadernacion": encuadernacion,
        "idioma": idioma,
        "paginas": paginas,
        "dimensiones": dimensiones,
        "fecha_publicacion": fecha_publicacion,
        "sku": sku,
        "descripcion_raw": descripcion_raw,
        "sinopsis": sinopsis,
        "raw_data": raw_data,
    }
