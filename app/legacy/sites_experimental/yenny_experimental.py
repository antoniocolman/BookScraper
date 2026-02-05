#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Scraper experimental de Yenny - El Ateneo.

Objetivo:
- Jugar con la extracción de datos de libros.
- Página 1: /libros/
- Páginas siguientes: /libros/page/{page}/?results_only=true&limit=12&theme=toluca
- De cada producto obtiene:
  título, autor, precio, ISBN, editorial, formato, encuadernación, idioma, SKU,
  sinopsis, URL de la imagen, etc.

Uso autónomo (módulo experimental):

    # Recorre hasta que no haya productos nuevos (orden default del sitio)
    python -m app.sites.experimentales.yenny_experimental --pages 0

    # Recorre todo ordenado alfabéticamente A-Z
    python -m app.sites.experimentales.yenny_experimental --pages 0 --sort-by alpha-ascending

    # Guarda CSV + descarga portadas
    python -m app.sites.experimentales.yenny_experimental --pages 2 --sort-by alpha-ascending \
        --output yenny_p1a2.csv --download-images --images-dir yenny_portadas
"""
from __future__ import annotations

import argparse
import csv
import re
import time
import random
import requests
from requests.exceptions import RequestException
from requests import Session
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from . import yenny_core as core  # shared helpers
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Configuración básica
# ---------------------------------------------------------------------------

BASE_URL = "https://yenny-elateneo.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

# Número máximo de páginas seguidas sin productos nuevos o vacías antes de cortar
MAX_EMPTY_PAGES = 2


def build_listing_url(page: int, sort_by: Optional[str] = None) -> str:
    """
    Construye la URL de listado según el número de página y el orden.

    Página 1:
        - Sin orden:   https://yenny-elateneo.com/libros/
        - Alfabético:  https://yenny-elateneo.com/libros/?sort_by=alpha-ascending

    Página 2+:
        - Sin orden:   https://yenny-elateneo.com/libros/page/{page}/?results_only=true&limit=12&theme=toluca
        - Alfabético:  ...page/{page}/?results_only=true&limit=12&theme=toluca&sort_by=alpha-ascending
    """
    if page <= 1:
        if sort_by:
            return f"{BASE_URL}/libros/?sort_by={sort_by}"
        return f"{BASE_URL}/libros/"

    base = f"{BASE_URL}/libros/page/{page}/?results_only=true&limit=12&theme=toluca"
    if sort_by:
        base += f"&sort_by={sort_by}"
    return base


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------


@dataclass
class BookRecord:
    source: str
    url: str

    isbn: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    price: Optional[float] = None
    currency: str = "ARS"

    editorial: Optional[str] = None
    formato: Optional[str] = None
    encuadernacion: Optional[str] = None
    idioma: Optional[str] = None
    paginas: Optional[int] = None
    fecha_publicacion: Optional[str] = None
    sku: Optional[str] = None

    # Sinopsis del libro (texto plano)
    synopsis: Optional[str] = None

    # URL de la imagen de portada
    image_url: Optional[str] = None

    # Bloque técnico crudo (Descripción)
    raw_data: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Utilidades HTTP
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    """Delegado a yenny_core."""
    return core.make_session(HEADERS)


def _extract_html_from_json_payload(data: Any) -> Optional[str]:
    """
    Si el endpoint de listado devuelve JSON con HTML embebido,
    buscamos el primer string que parezca contener tarjetas de productos.
    """
    if isinstance(data, str):
        if "/productos/" in data and "<a" in data:
            return data
        if "/productos/" in data and "<div" in data:
            return data
        return None

    if isinstance(data, dict):
        for value in data.values():
            result = _extract_html_from_json_payload(value)
            if result is not None:
                return result

    if isinstance(data, (list, tuple)):
        for item in data:
            result = _extract_html_from_json_payload(item)
            if result is not None:
                return result

    return None


def fetch_html(session: requests.Session, url: str, timeout: int = 20, results_only: bool = False) -> str:
    """Delegado a yenny_core (results_only se ignora si la URL ya lo trae)."""
    return core.fetch_html(session, url, timeout=timeout, results_only=results_only)

# ---------------------------------------------------------------------------
# Parsing de listados
# ---------------------------------------------------------------------------


def extract_product_urls_from_listing(html: str) -> List[str]:
    return core.extract_product_urls_from_listing(html, base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Parsing de ficha de producto
# ---------------------------------------------------------------------------


def _parse_price(text: Optional[str]) -> Optional[float]:
    """
    Convierte precios tipo "$37.999,00" -> 37999.00
    """
    if not text:
        return None

    m = re.search(r"\$?\s*([\d\.]+,\d{2})", text)
    if not m:
        return None

    num = m.group(1)
    num = num.replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _extract_title_and_author(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    """
    Intenta extraer título y autor:

    - Título: primer <h1>
    - Autor: primer <a> *después* del h1 (para evitar breadcrumbs).
    """
    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else None

    author: Optional[str] = None
    if title_tag is not None:
        next_a = title_tag.find_next("a")
        if isinstance(next_a, Tag):
            candidate = next_a.get_text(strip=True)
            if candidate and len(candidate.split()) >= 2:
                author = candidate

    return title, author


def _extract_sku_and_price(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[float]]:
    """
    - Busca el nodo de texto que contenga "SKU:"
    - A partir de ahí, busca el primer texto con "$" como precio principal.
    """
    text_nodes = soup.find_all(
        string=lambda t: isinstance(t, NavigableString) and "SKU:" in t
    )
    sku: Optional[str] = None
    price: Optional[float] = None

    if text_nodes:
        sku_text = text_nodes[0]
        sku = sku_text.split(":", 1)[-1].strip() or None

        current: Optional[NavigableString] = sku_text
        for _ in range(40):
            current = current.next_element  # type: ignore[attr-defined]
            if current is None:
                break
            if isinstance(current, NavigableString):
                if "$" in current:
                    price = _parse_price(str(current))
                    if price is not None:
                        break
    else:
        first_price_node = soup.find(
            string=lambda t: isinstance(t, NavigableString) and "$" in t
        )
        if first_price_node:
            price = _parse_price(str(first_price_node))

    return sku, price


def _extract_technical_block(soup: BeautifulSoup) -> Dict[str, str]:
    """
    Extrae el bloque de 'Descripción' con líneas tipo:

    Formato: LIBROS
    Editorial: Molino
    Encuadernación: Tapa Blanda
    Idioma: Español
    ISBN: 978...
    N° Páginas: 496
    Fecha Publicación: ...

    Devuelve un diccionario con las claves en minúsculas.
    """
    tech_data: Dict[str, str] = {}

    # Buscar encabezado "Descripción"
    desc_heading = soup.find(
        lambda tag: isinstance(tag, Tag)
        and tag.name in ("h3", "h4", "h5")
        and "Descripción" in tag.get_text()
    )
    if not desc_heading:
        return tech_data

    for sibling in desc_heading.find_all_next():
        if not isinstance(sibling, Tag):
            continue

        # Cortar cuando llegamos a Sinópsis / Sinopsis / Reseña, etc.
        if sibling.name in ("h3", "h4", "h5"):
            if any(
                k in sibling.get_text()
                for k in ("Sinópsis", "Sinopsis", "Reseña", "Sinopsis")
            ):
                break

        if sibling.name not in ("p", "li", "span", "div"):
            continue

        text = sibling.get_text(" ", strip=True)
        if not text or ":" not in text:
            continue

        key, value = [x.strip() for x in text.split(":", 1)]
        if not key or not value:
            continue

        tech_data[key.lower()] = value

    return tech_data


def _extract_synopsis(soup: BeautifulSoup) -> Optional[str]:
    """
    Extrae la sinopsis.

    Caso principal (el que mostraste):
    <p><strong>Sinópsis</strong><br>Texto largo...</p>

    Fallback: algún contenedor que tenga la palabra Sinopsis/Sinópsis y
    luego nodos hermanos con el texto.
    """
    # 1) Caso principal: <p> con <strong>Sinópsis/Sinopsis</strong> dentro
    for p in soup.find_all("p"):
        strong = p.find("strong")
        if not isinstance(strong, Tag):
            continue

        heading_text = strong.get_text(" ", strip=True).lower()
        if not re.search(r"sin[óo]psis", heading_text, re.IGNORECASE):
            continue

        # Tomamos todos los textos del <p> y quitamos el encabezado "Sinopsis"
        parts = list(p.stripped_strings)
        cleaned_parts: List[str] = []
        for t in parts:
            if re.search(r"^sin[óo]psis\s*:?$", t.strip(), re.IGNORECASE):
                continue
            cleaned_parts.append(t)

        synopsis = " ".join(cleaned_parts).strip()
        if synopsis:
            return synopsis

    # 2) Fallback: cualquier tag que contenga "sinopsis/sinópsis" y luego sus hermanos
    heading = soup.find(
        lambda tag: isinstance(tag, Tag)
        and re.search(
            r"sin[óo]psis",
            tag.get_text(" ", strip=True),
            re.IGNORECASE,
        )
    )
    if not heading:
        return None

    synopsis_parts: List[str] = []

    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag):
            # Si viene otra cabecera, cortamos
            if sibling.name in ("h2", "h3", "h4", "h5"):
                break

            text = " ".join(sibling.stripped_strings)
            if not text:
                continue
            if re.search(r"sin[óo]psis", text, re.IGNORECASE):
                continue

            synopsis_parts.append(text)

    synopsis = " ".join(synopsis_parts).strip()
    return synopsis or None


def _parse_srcset(srcset: str) -> List[str]:
    """
    Parsea un atributo srcset/data-srcset y devuelve solo las URLs
    en orden (de menor a mayor tamaño normalmente).
    """
    urls: List[str] = []
    for part in srcset.split(","):
        token = part.strip().split(" ", 1)[0]
        if token:
            urls.append(token)
    return urls


def _extract_image_url(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    """
    Intenta obtener la portada del libro.

    Priorizamos:
    1) meta og:image / twitter:image
    2) Contenedor del slider:
       <div class="js-product-slide ...">
           <a href="//acdn-us...-1024-1024.webp" data-fancybox="product-gallery">...</a>
           <img data-srcset="...480-0.webp 480w, ...640-0.webp 640w, ...1024-1024.webp 1024w">
    3) Fallback: primer <img> razonable dentro del contenido principal.
    """
    # 1) og:image
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return urljoin(page_url, og["content"].strip())

    # 2) twitter:image
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return urljoin(page_url, tw["content"].strip())

    # 3) Slider principal (lo que mostraste)
    slide = soup.find("div", class_="js-product-slide")
    if slide:
        # 3a) <a href="...1024-1024.webp" data-fancybox="product-gallery">
        link = slide.find("a", attrs={"data-fancybox": "product-gallery"})
        if link and link.get("href"):
            return urljoin(page_url, link["href"].strip())

        # 3b) data-srcset o srcset con varias resoluciones
        img = slide.find("img", attrs={"data-srcset": True}) or slide.find(
            "img", attrs={"srcset": True}
        )
        if img:
            srcset = img.get("data-srcset") or img.get("srcset") or ""
            candidates = _parse_srcset(srcset)
            if candidates:
                # normal: el último suele ser el de mayor tamaño (1024-1024.webp)
                return urljoin(page_url, candidates[-1])

        # 3c) Fallback dentro del slide: primer src
        img2 = slide.find("img", src=True)
        if img2:
            return urljoin(page_url, img2["src"].strip())

    # 4) Fallback genérico: algún img dentro del main/product
    main = (
        soup.find("div", class_="product-main")
        or soup.find("div", class_="product-detail")
        or soup.find("main")
        or soup
    )
    img = main.find("img") if main else None
    if img and img.get("src"):
        return urljoin(page_url, img["src"].strip())

    return None


def parse_product_page(html: str, url: str, source: str = "yenny_elateneo") -> BookRecord:
    """Parsea un producto usando el parser compartido y lo adapta al BookRecord de este módulo."""
    data = core.parse_product_page(html, url)

    return BookRecord(
        source=source,
        url=url,
        title=data.get("title"),
        author=data.get("author"),
        isbn=data.get("isbn"),
        price=data.get("price"),
        currency=data.get("currency") or "ARS",
        image_url=data.get("image_url") or data.get("portada_url"),
        synopsis=data.get("sinopsis"),
        format=data.get("formato"),
        publisher=data.get("editorial"),
        binding=data.get("encuadernacion"),
        language=data.get("idioma"),
        pages=data.get("paginas"),
        publish_date=data.get("fecha_publicacion"),
        sku=data.get("sku"),
        raw_data=data.get("raw_data") or {},
    )


# ---------------------------------------------------------------------------
# Scraper principal
# ---------------------------------------------------------------------------


def scrape_yenny_elateneo(
    max_pages: Optional[int] = None,
    delay: float = 1.5,
    session: Optional[requests.Session] = None,
    sort_by: Optional[str] = None,
    workers: int = 4,
) -> Iterable[BookRecord]:
    """
    Itera sobre páginas de /libros/ y devuelve BookRecord por cada producto.

    max_pages:
        - None: recorre páginas hasta que haya MAX_EMPTY_PAGES páginas seguidas
                sin productos nuevos (o vacías)
        - entero > 0: recorre hasta esa página (salvo error anterior)
    """
    own_session = False
    if session is None:
        session = make_session()
        own_session = True

    try:
        seen_urls: Set[str] = set()
        page = 1
        empty_pages = 0

        while True:
            # Límite máximo de páginas (si se especificó)
            if max_pages is not None and page > max_pages:
                print(f"[INFO] Se alcanzó el límite de páginas: {max_pages}.")
                break

            listing_url = build_listing_url(page, sort_by=sort_by)
            print(f"[LISTADO] Página {page} -> {listing_url}")

            try:
                html = fetch_html(session, listing_url)
            except Exception as e:
                print(f"[ERROR] No se pudo descargar listado {page}: {e}")
                break

            product_urls = extract_product_urls_from_listing(html)

            # Sin productos en esta página
            if not product_urls:
                empty_pages += 1
                print(
                    f"[INFO] Página {page} sin productos "
                    f"(vacías seguidas: {empty_pages}/{MAX_EMPTY_PAGES})."
                )
                if empty_pages >= MAX_EMPTY_PAGES:
                    print(
                        f"[INFO] Se alcanzó el máximo de páginas vacías "
                        f"({MAX_EMPTY_PAGES}). Cortando."
                    )
                    break

                page += 1
                time.sleep(delay)
                continue

            # Filtrar solo los nuevos respecto al conjunto global
            new_urls = [u for u in product_urls if u not in seen_urls]
            if not new_urls:
                print(
                    f"[INFO] Página {page} no aportó productos nuevos "
                    f"({len(product_urls)} productos, todos repetidos). Cortando."
                )
                break

            print(f"[INFO] {len(new_urls)} productos nuevos en página {page}.")
            for url in new_urls:
                seen_urls.add(url)
                print(f"  [PRODUCTO] {url}")

            # --- Descarga de fichas de producto ---
            if workers and workers > 1:
                # Modo concurrente: varios hilos en paralelo (I/O bound).
                def _task(u: str) -> Optional[BookRecord]:
                    try:
                        prod_html = fetch_html(session, u)
                        return parse_product_page(prod_html, u)
                    except Exception as e:
                        print(f"  [ERROR] Falló producto {u}: {e}")
                        return None

                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [executor.submit(_task, url) for url in new_urls]
                    for fut in as_completed(futures):
                        record = fut.result()
                        if record is not None:
                            yield record

                # Pausa entre páginas para no pegarle tan fuerte al sitio
                time.sleep(delay)
            else:
                # Modo secuencial (como antes)
                for url in new_urls:
                    try:
                        prod_html = fetch_html(session, url)
                        record = parse_product_page(prod_html, url)
                        yield record
                    except Exception as e:
                        print(f"  [ERROR] Falló producto {url}: {e}")

                    time.sleep(delay)

            # Avanzamos de página y reseteamos contador de páginas vacías
            page += 1
            empty_pages = 0

    finally:
        if own_session:
            session.close()


# ---------------------------------------------------------------------------
# CSV + descarga de imágenes + CLI
# ---------------------------------------------------------------------------


def write_csv(path: Path, records: Iterable[BookRecord]) -> None:
    records_list = list(records)
    if not records_list:
        print("[WARN] No hay registros para escribir.")
        return

    fieldnames = list(asdict(records_list[0]).keys())

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records_list:
            writer.writerow(asdict(r))

    print(f"[OK] CSV guardado en: {path} ({len(records_list)} filas)")


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "book"


def download_images(
    records: Iterable[BookRecord],
    session: requests.Session,
    images_dir: Path,
) -> None:
    images_dir.mkdir(parents=True, exist_ok=True)

    for r in records:
        if not r.image_url:
            continue

        if r.isbn:
            base_name = r.isbn.strip()
        elif r.title:
            base_name = _slugify(r.title)
        else:
            base_name = "book"

        parsed = urlparse(r.image_url)
        ext = Path(parsed.path).suffix or ".jpg"
        filename = f"{base_name}{ext}"
        dest = images_dir / filename

        if dest.exists():
            print(f"[IMG] Ya existe, salto: {dest}")
            continue

        try:
            resp = session.get(r.image_url, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[IMG] Error bajando {r.image_url}: {exc}")
            continue

        dest.write_bytes(resp.content)
        print(f"[IMG] Guardada: {dest}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scraper de libros de Yenny - El Ateneo (modo autónomo, experimental)."
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=0,
        help=(
            "Cantidad de páginas a recorrer. "
            "0 = hasta que se alcancen MAX_EMPTY_PAGES páginas seguidas "
            "sin productos nuevos (Ctrl+C para cortar manualmente). "
            "Default: 0."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Ruta de salida CSV. Si se omite, solo imprime algunos resultados.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Delay en segundos entre requests (default: 1.5).",
    )
    parser.add_argument(
        "--sort-by",
        type=str,
        default=None,
        help=(
            "Campo de ordenamiento del sitio. "
            "Ejemplo: 'alpha-ascending' para ordenar A-Z."
        ),
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="Si se indica, descarga las portadas en la carpeta indicada por --images-dir.",
    )
    parser.add_argument(
        "--images-dir",
        type=str,
        default="yenny_images",
        help="Carpeta donde se guardarán las portadas descargadas (default: yenny_images).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help=(
            "Número de hilos en paralelo para descargar fichas de producto. "
            "1 = modo secuencial. Default: 4."
        ),
    )

    args = parser.parse_args()

    max_pages: Optional[int] = args.pages if args.pages > 0 else None

    session = make_session()
    start = time.perf_counter()
    records_iter = scrape_yenny_elateneo(
        max_pages=max_pages,
        delay=args.delay,
        session=session,
        sort_by=args.sort_by,
        workers=args.workers,
    )
    records_list = list(records_iter)
    elapsed = time.perf_counter() - start
    print(
        f"[TIME] workers={args.workers}, pages={args.pages}, "
        f"registros={len(records_list)}, tiempo={elapsed:.2f}s"
    )

    if args.output:
        write_csv(Path(args.output), records_list)
    else:
        print(f"[INFO] Total registros: {len(records_list)}")
        for r in records_list[:20]:
            print("-" * 80)
            print(f"Título   : {r.title}")
            print(f"Autor    : {r.author}")
            print(f"ISBN     : {r.isbn}")
            print(f"Precio   : {r.price} {r.currency}")
            print(f"URL      : {r.url}")
            print(f"Imagen   : {r.image_url}")
            if r.synopsis:
                snippet = (r.synopsis[:200] + "...") if len(r.synopsis) > 200 else r.synopsis
                print(f"Sinopsis : {snippet}")
            else:
                print("Sinopsis : (sin datos)")

    if args.download_images and records_list:
        images_dir = Path(args.images_dir)
        download_images(records_list, session, images_dir)

    session.close()


if __name__ == "__main__":
    main()