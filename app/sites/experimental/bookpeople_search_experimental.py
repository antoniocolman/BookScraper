from __future__ import annotations

import argparse
import csv
import re
import time
import random
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Any, Dict

import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

BASE_URL = "https://www.bookpeople.com"

# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclass
class QueryItem:
    raw: str
    isbn: Optional[str]
    title: Optional[str]

@dataclass
class BookRecord:
    url: str
    title: Optional[str] = None
    author: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    isbn: Optional[str] = None
    pages: Optional[str] = None
    publisher: Optional[str] = None
    publish_date: Optional[str] = None
    publish_date_iso: Optional[str] = None
    image_url: Optional[str] = None
    binding: Optional[str] = None
    description: Optional[str] = None
    about_author: Optional[str] = None
    description_raw_html: Optional[str] = None
    formats_raw: Optional[List[Dict[str, Any]]] = None

@dataclass
class MatchResult:
    # Consulta original
    query_raw: str
    isbn_archivo: Optional[str]
    titulo_archivo: Optional[str]

    # Datos BookPeople
    isbn_bookpeople: Optional[str]
    titulo_bookpeople: Optional[str]
    autor_bookpeople: Optional[str]

    precio_bookpeople: Optional[float]
    moneda_bookpeople: Optional[str]

    url_bookpeople: Optional[str]
    imagen_bookpeople: Optional[str]

    paginas_bookpeople: Optional[str]
    encuadernacion_bookpeople: Optional[str]
    editorial_bookpeople: Optional[str]
    fecha_publicacion_bookpeople: Optional[str]
    fecha_publicacion_iso_bookpeople: Optional[str]

    descripcion_bookpeople: Optional[str]
    acerca_autor_bookpeople: Optional[str]
    descripcion_raw_html_bookpeople: Optional[str]

    # Matching / scoring
    isbn_match: bool
    titulo_match_estricto: bool
    titulo_match_relajado: bool
    similitud_titulo: float

    motivo_seleccion: str

# -----------------------------------------------------------------------------
# Utilidades de normalización
# -----------------------------------------------------------------------------

def _normalize_isbn(isbn: Optional[str]) -> Optional[str]:
    if not isbn:
        return None
    digits = re.sub(r"[^0-9Xx]", "", isbn)
    return digits or None

def _normalize_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = title.lower()
    # quitar acentos básicos
    t = (
        t.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
    )
    t = t.replace("-", " ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None

# -----------------------------------------------------------------------------
# Lectura de archivo de consultas
# -----------------------------------------------------------------------------

def parse_query_line(line: str) -> Optional[QueryItem]:
    """
    Igual estilo que en yenny_search_experimental:
    - Soporta "ISBN | TÍTULO" (pipe o tab)
    - Soporta solo ISBN
    - Soporta solo título
    """
    raw = line.strip()
    if not raw:
        return None
    if raw.startswith("#"):
        return None
    if raw.isdigit() and len(raw) <= 3:
        # líneas tipo "1", "2" de numeración simple
        return None

    isbn: Optional[str] = None
    title: Optional[str] = None

    sep = None
    if "|" in raw:
        sep = "|"
    elif "\t" in raw:
        sep = "\t"

    if sep:
        left, right = raw.split(sep, 1)
        left = left.strip()
        right = right.strip()

        # asumimos que la parte izquierda suele ser ISBN
        if left:
            digits = re.sub(r"[^0-9Xx]", "", left)
            if len(digits) >= 8:
                isbn = digits

        if right:
            digits_r = re.sub(r"[^0-9Xx]", "", right)
            if isbn is None and len(digits_r) >= 8:
                isbn = digits_r
            else:
                title = right
    else:
        # línea simple: ISBN o título suelto (o incluso una URL)
        digits = re.sub(r"[^0-9Xx]", "", raw)
        if len(digits) >= 8:
            isbn = digits
        else:
            title = raw

    if not isbn and not title:
        return None

    return QueryItem(raw=raw, isbn=isbn, title=title)

def load_queries_from_file(path: Path, limit: Optional[int] = None) -> List[QueryItem]:
    if not path.is_file():
        print(f"[WARN] Archivo de consultas no existe: {path}")
        return []

    queries: List[QueryItem] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            q = parse_query_line(line)
            if not q:
                continue
            queries.append(q)
            if limit is not None and len(queries) >= limit:
                break

    print(f"[INFO] Cargadas {len(queries)} consultas desde {path}")
    return queries

# -----------------------------------------------------------------------------
# HTTP helpers y manejo de 429
# -----------------------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": BASE_URL + "/",
        }
    )
    return session


def fetch_html(session: requests.Session, url: str, *, timeout: float = 30.0) -> str:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    if not resp.encoding:
        resp.encoding = resp.apparent_encoding
    return resp.text


def fetch_html_with_backoff(
    session: requests.Session,
    url: str,
    *,
    max_retries: int = 3,
    base_wait: float = 5.0,
) -> str:
    """
    Wrapper de fetch_html que intenta manejar 429 con backoff progresivo.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            return fetch_html(session, url)
        except requests.HTTPError as exc:  # type: ignore[attr-defined]
            status = getattr(exc.response, "status_code", None)
            if status == 429 and attempt < max_retries:
                factor = attempt
                jitter = random.uniform(0.8, 1.2)
                wait = base_wait * factor * jitter
                print(
                    f"[WARN] 429 Too Many Requests en {url} "
                    f"(intento {attempt}/{max_retries}). Esperando {wait:.1f}s..."
                )
                time.sleep(wait)
                last_exc = exc
                continue
            last_exc = exc
            break
        except Exception as exc:
            last_exc = exc
            break

    if last_exc:
        raise last_exc

    return fetch_html(session, url)


# -----------------------------------------------------------------------------
# Parsing HTML
# -----------------------------------------------------------------------------


def _normalize_url(href: str) -> str:
    href = href.strip()
    if not href:
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return BASE_URL.rstrip("/") + "/" + href.lstrip("/")


def extract_product_urls_from_listing(html: str) -> List[str]:
    """
    Extrae URLs de libros desde la página de resultados de búsqueda de BookPeople.

    Estrategia genérica: cualquier <a> cuyo href empiece con "/book/".
    """
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/book/"):
            urls.add(_normalize_url(href))

    return list(urls)


def _extract_variation_field(soup: BeautifulSoup, token: str) -> Optional[str]:
    """
    Busca divs con class que contiene el token:
    e.g. token='variation_field_publisher__' matchea:
    'product--variation-field--variation_field_publisher__1158362'
    """
    node = soup.select_one(f"div[class*='{token}']")
    if not node:
        return None

    # suele venir: <div class="product-details__label">Publisher:</div>VALOR
    label = node.select_one(".product-details__label")
    texts = [t.strip() for t in node.stripped_strings if t and t.strip()]
    if not texts:
        return None

    if label:
        lab = label.get_text(strip=True)
        # normalmente lab es el primer "string"
        if texts and texts[0].startswith(lab):
            texts = texts[1:]
    else:
        # fallback: si el primer elemento parece label "Publisher:" / "ISBN:" / etc
        if len(texts) > 1 and texts[0].endswith(":"):
            texts = texts[1:]

    val = " ".join(texts).strip()
    return val or None


def _parse_binding_option_text(s: str) -> tuple[Optional[str], Optional[str]]:
    """
    'Paperback (11/17/2025)' -> ('Paperback', '2025-11-17')
    Si no hay fecha, devuelve (binding, None)
    """
    if not s:
        return None, None
    s = " ".join(s.split()).strip()
    # binding antes del paréntesis
    m = re.match(r"^(.*?)\s*\((\d{1,2})/(\d{1,2})/(\d{4})\)\s*$", s)
    if m:
        binding = m.group(1).strip() or None
        mm = int(m.group(2))
        dd = int(m.group(3))
        yy = int(m.group(4))
        date_iso = f"{yy:04d}-{mm:02d}-{dd:02d}"
        return binding, date_iso
    # sin fecha
    return (s or None), None


def _extract_available_formats(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str], list]:
    """
    Lee el <select> 'Available Formats' y devuelve:
    - binding seleccionado (Paperback/Hardcover/...)
    - binding_date_iso (si viene en el texto)
    - formats_raw: lista de opciones con value/label/selected
    """
    sel = soup.select_one("select#edit-related-products, select[name='related_products']")
    if not sel:
        return None, None, []

    formats = []
    selected_binding = None
    selected_date_iso = None

    for opt in sel.find_all("option"):
        label = opt.get_text(" ", strip=True)
        value = (opt.get("value") or "").strip()
        is_selected = opt.has_attr("selected")

        binding, date_iso = _parse_binding_option_text(label)

        formats.append(
            {
                "value": value,          # puede ser ISBN o URL externa
                "label": label,
                "binding": binding,
                "date_iso": date_iso,
                "selected": bool(is_selected),
            }
        )

        if is_selected:
            selected_binding = binding
            selected_date_iso = date_iso

    return selected_binding, selected_date_iso, formats

def parse_product_page(html: str, url: str) -> BookRecord:
    soup = BeautifulSoup(html, "html.parser")

    # Título
    title_el = soup.select_one("h1.product-details__title")
    title = title_el.get_text(strip=True) if title_el else None

    # Autor (suele ser link /search?type=author...)
    author_el = soup.select_one("a[href^='/search?type=author']")
    if not author_el:
        author_el = soup.select_one(".product-details__contributors a, .product-details__author a")
    author = author_el.get_text(strip=True) if author_el else None

    # Imagen (portada)
    img_el = soup.select_one("img[src*='images.booksense.com']")
    if not img_el:
        img_el = soup.select_one(".product-details__image img, .product-details__media img")
    image_url = (img_el.get("src") or "").strip() if img_el else None
    if image_url and image_url.startswith("//"):
        image_url = "https:" + image_url

    # Campos por variation_field_*
    publisher = _extract_variation_field(soup, "variation_field_publisher__")
    isbn = _extract_variation_field(soup, "variation_field_isbn_13__")
    pages = _extract_variation_field(soup, "variation_field_pages__")
    language = _extract_variation_field(soup, "variation_field_language__")

    # Publish Date: guardar ISO corto YYYY-MM-DD (DB-friendly)
    publish_date = None
    publish_date_iso = None
    time_el = soup.select_one("div.product-details__pub-date time[datetime]")
    if not time_el:
        time_el = soup.select_one("time[datetime]")  # fallback
    if time_el:
        publish_date = time_el.get_text(strip=True) or None
        dt_attr = (time_el.get("datetime") or "").strip()
        # '2025-11-18T00:00:00-06:00' -> '2025-11-18'
        if re.match(r"^\d{4}-\d{2}-\d{2}", dt_attr):
            publish_date_iso = dt_attr[:10]

    # Precio (mantenemos tu selector)
    price = None
    currency = None
    price_el = soup.select_one(".product-details__price, .browse-books__price-sale, .product-details__price-sale")
    if price_el:
        price_text = price_el.get_text(" ", strip=True)
        m = re.search(r"\$([0-9]+(?:\.[0-9]{1,2})?)", price_text)
        if m:
            try:
                price = float(m.group(1))
                currency = "USD"
            except Exception:
                pass

    # Descripción/Sinopsis: ampliar fallbacks
    description = None
    description_raw_html = None

    # 1) tus heurísticas previas (si existen), 2) field--name-body, 3) primer bloque grande de texto
    desc_container = (
        soup.select_one(".field--name-body")
        or soup.select_one(".product-details__description")
        or soup.select_one(".product-details__desc")
        or soup.select_one("div[class*='product-details__description']")
    )
    if desc_container:
        description_raw_html = str(desc_container)
        # preserva saltos por <br>
        description = desc_container.get_text("\n", strip=True) or None

    # About author (si existe)
    about_author = None
    about_el = soup.select_one(".product-details__about-author, .field--name-field-about-author")
    if about_el:
        about_author = about_el.get_text("\n", strip=True) or None

    # Formato/tapa (Available Formats)
    binding, binding_date_iso, formats_raw = _extract_available_formats(soup)
    
    # ---------------- Encuadernación / Binding ----------------
    binding = None
    sel = soup.select_one("select#edit-related-products, select[name='related_products']")
    if sel:
        # preferimos el option seleccionado
        opt = sel.select_one("option[selected]") or sel.find("option", selected=True)
        options = sel.find_all("option")

        def clean_opt_text(t: str) -> str:
            t = (t or "").strip()
            # "Paperback (8/28/2006)" -> "Paperback"
            t = re.sub(r"\s*\(.*\)\s*$", "", t).strip()
            return t

        chosen = clean_opt_text(opt.get_text(" ", strip=True)) if opt else ""

        # Si el seleccionado es digital/ebook, intentamos elegir otro no-digital
        def is_digital(t: str) -> bool:
            tt = (t or "").lower()
            return ("ebook" in tt) or ("e-book" in tt) or ("digital" in tt)

        if chosen and not is_digital(chosen):
            binding = chosen
        else:
            for o in options:
                t = clean_opt_text(o.get_text(" ", strip=True))
                if t and not is_digital(t):
                    binding = t
                    break

    return BookRecord(
        url=url,
        title=title,
        author=author,
        price=price,
        currency=currency,
        isbn=isbn,
        pages=pages,
        publisher=publisher,
        publish_date=publish_date,
        publish_date_iso=publish_date_iso,
        image_url=image_url,
        binding=binding,
        description=description,
        about_author=about_author,
        description_raw_html=description_raw_html,
        formats_raw=formats_raw,
    )

# -----------------------------------------------------------------------------
# Lógica de búsqueda y ranking de resultados
# -----------------------------------------------------------------------------

def search_bookpeople(
    session: requests.Session,
    query: QueryItem,
    *,
    max_results: int = 5,
    delay: float = 1.5,
    max_retries: int = 3,
    base_wait: float = 5.0,
) -> Optional[MatchResult]:
    """
    Lógica principal de búsqueda:
    - Si la línea original parece URL -> se usa tal cual.
    - Si hay ISBN -> primero intentamos /book/{ISBN}, luego (si hace falta) búsqueda.
    - Si solo hay título -> usamos búsqueda.
    """
    raw = query.raw.strip()

    # ------------------------------------------------------------------
    # 1) Caso URL directa
    # ------------------------------------------------------------------
    direct_urls: List[str] = []
    if raw.lower().startswith("http://") or raw.lower().startswith("https://"):
        direct_urls.append(raw)

    # ------------------------------------------------------------------
    # 2) Caso ISBN -> /book/{isbn}
    # ------------------------------------------------------------------
    expected_isbn_norm = _normalize_isbn(query.isbn)
    if expected_isbn_norm:
        direct_urls.append(f"{BASE_URL}/book/{expected_isbn_norm}")

    candidates: List[Tuple[BookRecord, bool]] = []  # (record, from_direct)

    # Intentar URLs directas primero
    for u in direct_urls:
        try:
            print(f"[DIRECT] Intentando URL directa: {u}")
            detail_html = fetch_html_with_backoff(
                session,
                u,
                max_retries=max_retries,
                base_wait=base_wait,
            )
            record = parse_product_page(detail_html, u)
            candidates.append((record, True))
        except Exception as e:
            print(f"[WARN] No se pudo obtener/parsing directo de {u}: {e}")

    # Si ya tenemos un candidato directo con ISBN exacto, podemos devolverlo
    for record, from_direct in candidates:
        if expected_isbn_norm and _normalize_isbn(record.isbn) == expected_isbn_norm:
            print("[INFO] Coincidencia directa por /book/{ISBN}.")
            return _build_match_result(query, record, from_direct, expected_isbn_norm)

    # ------------------------------------------------------------------
    # 3) Búsqueda por título o ISBN
    # ------------------------------------------------------------------
    search_q: Optional[str] = None
    if query.title:
        search_q = query.title
    elif expected_isbn_norm:
        search_q = expected_isbn_norm

    if search_q:
        # BookPeople (actual): /search?q=<QUERY>[&page=N]
        encoded = urllib.parse.quote_plus(search_q)
        search_url = f"{BASE_URL}/search?q={encoded}"
        print(f"[SEARCH] '{search_q}' -> {search_url}")
        try:
            html = fetch_html_with_backoff(
                session,
                search_url,
                max_retries=max_retries,
                base_wait=base_wait,
            )
            product_urls = extract_product_urls_from_listing(html)
        except Exception as e:
            print(f"[ERROR] Falló la búsqueda para '{search_q}': {e}")
            product_urls = []

        if not product_urls:
            print(f"[INFO] Sin resultados visibles para '{search_q}'.")

        # Limitar cantidad de productos a parsear
        product_urls = product_urls[:max_results]

        for idx, url in enumerate(product_urls, start=1):
            if any(url == rec.url for rec, _ in candidates):
                continue  # ya lo tenemos por URL directa
            print(f"  [PRODUCTO {idx}] {url}")
            try:
                time.sleep(delay)
                detail_html = fetch_html_with_backoff(
                    session,
                    url,
                    max_retries=max_retries,
                    base_wait=base_wait,
                )
                record = parse_product_page(detail_html, url)
                candidates.append((record, False))
            except Exception as e:
                print(f"    [ERROR] No se pudo parsear {url}: {e}")
                continue

    if not candidates:
        print(f"[INFO] Ningún producto válido para '{query.raw}'.")
        return None

    # ------------------------------------------------------------------
    # 4) Scoring de candidatos (igual espíritu que en Yenny)
    # ------------------------------------------------------------------
    expected_title_norm = _normalize_title(query.title)

    best_score = -1.0
    best_record: Optional[BookRecord] = None
    best_flags: Tuple[bool, bool, bool, float, str] = (False, False, False, 0.0, "")
    best_from_direct = False

    for record, from_direct in candidates:
        isbn_norm = _normalize_isbn(record.isbn)
        title_norm = _normalize_title(record.title)

        isbn_match = bool(
            expected_isbn_norm and isbn_norm and expected_isbn_norm == isbn_norm
        )

        titulo_match_estricto = bool(
            expected_title_norm and title_norm and expected_title_norm == title_norm
        )

        titulo_match_relajado = bool(
            expected_title_norm
            and title_norm
            and (
                expected_title_norm in title_norm
                or title_norm in expected_title_norm
            )
        )

        similitud = 0.0
        if expected_title_norm and title_norm:
            similitud = SequenceMatcher(None, expected_title_norm, title_norm).ratio()

        score = 0.0
        motivo: List[str] = []

        if isbn_match:
            score += 100.0
            motivo.append("ISBN exacto")
        if titulo_match_estricto:
            score += 20.0
            motivo.append("título exacto")
        if titulo_match_relajado:
            score += 5.0
            motivo.append("título relajado")
        score += similitud

        if from_direct:
            score += 10.0
            motivo.append("URL directa")

        motivo_str = ", ".join(motivo) if motivo else "mejor candidato por similitud"

        print(
            f"    [CANDIDATO] ISBN={record.isbn} | "
            f"Título='{record.title}' | score={score:.3f} ({motivo_str})"
        )

        if score > best_score:
            best_score = score
            best_record = record
            best_flags = (
                isbn_match,
                titulo_match_estricto,
                titulo_match_relajado,
                similitud,
                motivo_str,
            )
            best_from_direct = from_direct

    if not best_record:
        print(f"[INFO] No se pudo determinar mejor candidato para '{query.raw}'.")
        return None

    return _build_match_result(
        query,
        best_record,
        best_from_direct,
        expected_isbn_norm,
        flags=best_flags,
    )


def _build_match_result(
    query: QueryItem,
    record: BookRecord,
    from_direct: bool,
    expected_isbn_norm: Optional[str],
    flags: Optional[Tuple[bool, bool, bool, float, str]] = None,
) -> MatchResult:
    if flags is None:
        isbn_norm = _normalize_isbn(record.isbn)
        title_norm = _normalize_title(record.title)
        expected_title_norm = _normalize_title(query.title)

        isbn_match = bool(
            expected_isbn_norm and isbn_norm and expected_isbn_norm == isbn_norm
        )
        titulo_match_estricto = bool(
            expected_title_norm and title_norm and expected_title_norm == title_norm
        )
        titulo_match_relajado = bool(
            expected_title_norm
            and title_norm
            and (
                expected_title_norm in title_norm
                or title_norm in expected_title_norm
            )
        )
        similitud = 0.0
        if expected_title_norm and title_norm:
            similitud = SequenceMatcher(None, expected_title_norm, title_norm).ratio()

        motivo_elems = []
        if isbn_match:
            motivo_elems.append("ISBN exacto")
        if titulo_match_estricto:
            motivo_elems.append("título exacto")
        if titulo_match_relajado:
            motivo_elems.append("título relajado")
        if from_direct:
            motivo_elems.append("URL directa")
        motivo = ", ".join(motivo_elems) if motivo_elems else "seleccionado por similitud"

        flags = (
            isbn_match,
            titulo_match_estricto,
            titulo_match_relajado,
            similitud,
            motivo,
        )

    (
        isbn_match_flag,
        titulo_estricto_flag,
        titulo_relajado_flag,
        similitud_val,
        motivo_val,
    ) = flags

    result = MatchResult(
        query_raw=query.raw,
        isbn_archivo=query.isbn,
        titulo_archivo=query.title,
        isbn_bookpeople=record.isbn,
        titulo_bookpeople=record.title,
        autor_bookpeople=record.author,
        precio_bookpeople=record.price,
        moneda_bookpeople=record.currency,
        url_bookpeople=record.url,
        imagen_bookpeople=record.image_url,
        paginas_bookpeople=record.pages,
        encuadernacion_bookpeople=record.binding,
        editorial_bookpeople=record.publisher,
        fecha_publicacion_bookpeople=record.publish_date,
        fecha_publicacion_iso_bookpeople=record.publish_date_iso,
        descripcion_bookpeople=record.description,
        acerca_autor_bookpeople=record.about_author,
        descripcion_raw_html_bookpeople=record.description_raw_html,
        isbn_match=isbn_match_flag,
        titulo_match_estricto=titulo_estricto_flag,
        titulo_match_relajado=titulo_relajado_flag,
        similitud_titulo=similitud_val,
        motivo_seleccion=motivo_val,
    )

    # Resumen en consola
    print("-" * 80)
    print(f"Título archivo : {query.title}")
    print(f"ISBN archivo   : {query.isbn}")
    print(f"Título BP      : {result.titulo_bookpeople}")
    print(f"ISBN BP        : {result.isbn_bookpeople}")
    print(f"Autor BP       : {result.autor_bookpeople}")
    print(f"Precio BP      : {result.precio_bookpeople} {result.moneda_bookpeople}")
    print(f"Páginas BP     : {result.paginas_bookpeople}")
    print(f"Editorial BP   : {result.editorial_bookpeople}")
    print(f"Encuadernación : {result.encuadernacion_bookpeople}")
    print(f"F. Pub. BP     : {result.fecha_publicacion_bookpeople} " f"({result.fecha_publicacion_iso_bookpeople})")
    print(f"URL BP         : {result.url_bookpeople}")
    print(f"Imagen BP      : {result.imagen_bookpeople}")
    print(f"Descripción    : {bool(result.descripcion_bookpeople)}")
    print(f"Acerca autor   : {bool(result.acerca_autor_bookpeople)}")
    print(f"ISBN match     : {result.isbn_match}")
    print(f"Título estricto: {result.titulo_match_estricto}")
    print(f"Título relajado: {result.titulo_match_relajado}")
    print(f"Similitud tit. : {result.similitud_titulo:.3f}")
    print(f"Motivo         : {result.motivo_seleccion}")
    print("=" * 80)

    return result

# -----------------------------------------------------------------------------
# CSV de salida
# -----------------------------------------------------------------------------

def write_results_csv(path: Path, results: Iterable[MatchResult]) -> None:
    results_list = list(results)
    if not results_list:
        print("[WARN] No hay resultados para escribir en CSV.")
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "query_raw",
        "isbn_archivo",
        "titulo_archivo",
        "isbn_bookpeople",
        "titulo_bookpeople",
        "autor_bookpeople",
        "precio_bookpeople",
        "moneda_bookpeople",
        "url_bookpeople",
        "imagen_bookpeople",
        "paginas_bookpeople",
        "encuadernacion_bookpeople",
        "editorial_bookpeople",
        "fecha_publicacion_bookpeople",
        "fecha_publicacion_iso_bookpeople",
        "descripcion_bookpeople",
        "acerca_autor_bookpeople",
        "descripcion_raw_html_bookpeople",
        "isbn_match",
        "titulo_match_estricto",
        "titulo_match_relajado",
        "similitud_titulo",
        "motivo_seleccion",
    ]

    print(f"[INFO] Escribiendo CSV en {path}")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results_list:
            row = asdict(r)
            writer.writerow(row)

    print(f"[CSV] {len(results_list)} filas escritas en {path}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Búsqueda directa en BookPeople por ISBN / título / URL (experimental)."
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Consulta única (ISBN, título o URL directa a /book/...).",
    )
    parser.add_argument(
        "--query-file",
        type=str,
        default=None,
        help=(
            "Ruta a archivo .txt con una consulta por línea. "
            "Formato recomendado: 'ISBN | TÍTULO'. "
            "También acepta solo ISBN, solo título o URL."
        ),
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Máximo de productos a evaluar por consulta (cuando se usa búsqueda).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Pausa en segundos entre descargas de fichas de producto (modo búsqueda).",
    )
    parser.add_argument(
        "--query-delay",
        type=float,
        default=0.0,
        help="Pausa en segundos entre consultas del archivo.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Reintentos máximos por request ante errores HTTP (incluyendo 429).",
    )
    parser.add_argument(
        "--base-wait",
        type=float,
        default=5.0,
        help="Espera base (segundos) antes de reintentar tras 429.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Si >0, cantidad de consultas a procesar antes de hacer una pausa larga.",
    )
    parser.add_argument(
        "--batch-pause",
        type=float,
        default=0.0,
        help="Duración en segundos de la pausa larga entre tandas de consultas.",
    )
    parser.add_argument(
        "--limit-queries",
        type=int,
        default=None,
        help="Si se usa --query-file, limita a las primeras N consultas válidas.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="bookpeople_busqueda_resultados.csv",
        help="Ruta del CSV de salida (abrible con Excel).",
    )
    return parser


def main() -> None:
    parser = build_argparser()
    args = parser.parse_args()

    queries: List[QueryItem] = []

    if args.query_file:
        file_path = Path(args.query_file)
        queries = load_queries_from_file(file_path, limit=args.limit_queries)
    elif args.query:
        q = parse_query_line(args.query)
        if q:
            queries = [q]
    else:
        print("[ERROR] Debes usar --query o --query-file.")
        return

    if not queries:
        print("[ERROR] No se encontró ninguna consulta válida.")
        return

    session = make_session()
    results: List[MatchResult] = []

    try:
        for idx, q in enumerate(queries, start=1):
            # Pausa larga por tandas (batch)
            if (
                args.batch_size > 0
                and args.batch_pause > 0
                and idx > 1
                and (idx - 1) % args.batch_size == 0
            ):
                print(
                    f"[INFO] Pausa de lote: ya se procesaron {idx-1} consultas, "
                    f"durmiendo {args.batch_pause:.1f}s..."
                )
                time.sleep(args.batch_pause)

            # Pausa corta entre consultas
            if idx > 1 and args.query_delay > 0:
                time.sleep(args.query_delay)

            print("=" * 80)
            print(f"[QUERY #{idx}] {q.raw}")

            res = search_bookpeople(
                session,
                q,
                max_results=args.max_results,
                delay=args.delay,
                max_retries=args.max_retries,
                base_wait=args.base_wait,
            )
            if res:
                results.append(res)
    finally:
        session.close()

    write_results_csv(Path(args.output), results)


if __name__ == "__main__":
    main()
