from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit
import httpx

try:
    from app.storage.book_std_db import connect, init_db, upsert_many  # type: ignore
except Exception:  # pragma: no cover
    connect = init_db = upsert_many = None  # type: ignore

from app.config import EXPORTS_DIR, DB_PATH, STATE_DIR
DEFAULT_OUT_STD = str(EXPORTS_DIR / "bookpeople_catalog_std.csv")
DEFAULT_OUT_FULL = str(EXPORTS_DIR / "bookpeople_catalog_full.csv")
DEFAULT_DB = str(DB_PATH)
DEFAULT_SEEN_FILE = str(STATE_DIR / "bookpeople_catalog_seen_urls.txt")

# -----------------------
# Config
# -----------------------
BASE = "https://bookpeople.com"
CATALOG_PATH = "/books"

STD_FIELDS = [
    "ISBN",
    "TITULO",
    "AUTOR",
    "EDITORIAL",
    "SINOPSIS",
    "IDIOMA",
    "PAGINAS",
    "DIMENSIONES",
    "FECHA PUBLICACION",
    "URL",
    "URL PORTADA",
    "SITE",
]

# -----------------------
# selectolax (opcional, recomendado)
# `.text(strip=True)` está documentado por selectolax (strip, separator, deep, etc.). :contentReference[oaicite:0]{index=0}
# -----------------------
try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser  # type: ignore
except Exception:  # pragma: no cover
    HTMLParser = None  # type: ignore

# -----------------------
# Helpers
# -----------------------
_RE_ISBN_IN_URL = re.compile(r"/book/(\d{10,13}X?)", re.IGNORECASE)
_RE_SPACES = re.compile(r"\s+")
_RE_LAST_PAGE = re.compile(r'pager__item--last.*?href="[^"]*?[?&]page=(\d+)"', re.I | re.S)

_RE_ARTICLE = re.compile(
    r'<article[^>]+class="[^"]*\bbrowse-books__wrapper\b[^"]*"[^>]*>.*?</article>',
    re.I | re.S,
)
_RE_ARTICLE_ID = re.compile(r'id="product-variation-wrapper--(\d+)"', re.I)
_RE_HREF_BOOK = re.compile(r'href="(/book/[^"]+)"', re.I)
_RE_TITLE = re.compile(
    r'<h2[^>]+class="[^"]*\bbrowse-books__title\b[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>',
    re.I | re.S,
)
_RE_AUTHOR = re.compile(r'<div[^>]+class="[^"]*\bbrowse-books__author\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_RE_PRICE_BLOCK = re.compile(r'<span[^>]+class="[^"]*\bbrowse-books__price-sale\b[^"]*"[^>]*>(.*?)</span>', re.I | re.S)
_RE_IMG = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_RE_LABEL = re.compile(r'<div[^>]+class="[^"]*\bbrowse-books__book-label\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_RE_AVAIL = re.compile(r'<div[^>]+class="[^"]*\bproduct-details__availability-check\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_RE_ATC = re.compile(r'<a[^>]+class="[^"]*\badd-to-cart-link\b[^"]*"[^>]+href="([^"]+)"', re.I | re.S)


def _clean_text(s: str) -> str:
    s = s or ""
    s = s.replace("\xa0", " ")
    s = _RE_SPACES.sub(" ", s)
    return s.strip()


def _strip_tags(s: str) -> str:
    # “good enough” para fallback regex (el HTML de esa view es bastante prolijo)
    s = re.sub(r"<[^>]+>", " ", s or "")
    return _clean_text(s)

def _isbn_from_book_href(href: str) -> str:
    m = _RE_ISBN_IN_URL.search(href or "")
    return (m.group(1) if m else "").strip()

def _parse_price_usd(txt: str) -> Optional[float]:
    t = _clean_text(txt).replace("$", "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d{1,2})?)", t)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def _catalog_url(page: int) -> str:
    # page es 0-index (como Drupal)
    return f"{BASE}{CATALOG_PATH}?page={page}"

def _make_client() -> httpx.Client:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    return httpx.Client(headers=headers, http2=True, follow_redirects=True, timeout=30.0)

def _get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
) -> str:
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            r = client.get(url, timeout=timeout)
            # Reintentar explícitamente en throttling / server errors
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                sleep_s = (retry_backoff ** attempt) + random.uniform(0, 0.35)
                print(f"[WARN] GET falló (attempt {attempt+1}/{max_retries+1}) {url} -> {e} | sleep {sleep_s:.2f}s")
                time.sleep(sleep_s)
                continue
            raise RuntimeError(f"No se pudo descargar {url}. Último error: {last_exc}") from last_exc
    raise RuntimeError("Unreachable")

def _detect_last_page(html: str) -> int:
    """
    Devuelve el último índice de página (0-index). Ej: si hay 20 páginas, devuelve 19.
    """
    # 1) por selectolax
    if HTMLParser is not None:
        try:
            tree = HTMLParser(html)
            a = tree.css_first("li.pager__item--last a")
            if a is not None:
                href = a.attributes.get("href", "")
                qs = parse_qs(urlsplit(href).query)
                if "page" in qs and qs["page"]:
                    return int(qs["page"][0])
        except Exception:
            pass

    # 2) fallback regex
    m = _RE_LAST_PAGE.search(html or "")
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    # 3) si no encontramos, asumimos solo page=0
    return 0

def _parse_page_items_selectolax(html: str) -> List[Dict[str, Any]]:
    tree = HTMLParser(html)
    out: List[Dict[str, Any]] = []

    for art in tree.css("article.browse-books__wrapper"):
        href_book = ""
        for a in art.css("a"):
            h = a.attributes.get("href", "") or ""
            if h.startswith("/book/"):
                href_book = h
                break
        if not href_book:
            continue

        isbn = _isbn_from_book_href(href_book)
        detail_url = urljoin(BASE, href_book)

        title_node = art.css_first("h2.browse-books__title a[href^='/book/']")
        title = _clean_text(title_node.text(strip=True) if title_node else "")

        author_node = art.css_first("div.browse-books__author")
        author = _clean_text(author_node.text(strip=True) if author_node else "")

        price_node = art.css_first("span.browse-books__price-sale")
        price_txt = _clean_text(price_node.text(strip=True) if price_node else "")
        price = _parse_price_usd(price_txt)

        img_node = art.css_first("img")
        img = (img_node.attributes.get("src", "") if img_node else "") or ""
        if "no_cover.jpg" in img:
            img = ""

        label_node = art.css_first(".browse-books__book-label")
        label = _clean_text(label_node.text(strip=True) if label_node else "")

        avail_node = art.css_first(".product-details__availability-check")
        availability = _clean_text(avail_node.text(strip=True) if avail_node else "")

        atc_node = art.css_first("a.add-to-cart-link")
        add_to_cart = urljoin(BASE, atc_node.attributes.get("href", "")) if atc_node else ""

        variation_id = ""
        art_id = art.attributes.get("id", "") or ""
        mvid = re.search(r"--(\d+)$", art_id)
        if mvid:
            variation_id = mvid.group(1)

        out.append(
            {
                "isbn": isbn,
                "title": title,
                "author": author,
                "price_usd": price,
                "detail_url": detail_url,
                "image_url": img,
                "label": label,
                "availability": availability,
                "variation_id": variation_id,
                "add_to_cart": add_to_cart,
            }
        )

    return out

def _parse_page_items_regex(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for art_html in _RE_ARTICLE.findall(html or ""):
        href_m = _RE_HREF_BOOK.search(art_html)
        if not href_m:
            continue
        href_book = href_m.group(1)

        isbn = _isbn_from_book_href(href_book)
        detail_url = urljoin(BASE, href_book)

        title_m = _RE_TITLE.search(art_html)
        title = _strip_tags(title_m.group(1)) if title_m else ""

        author_m = _RE_AUTHOR.search(art_html)
        author = _strip_tags(author_m.group(1)) if author_m else ""

        price_m = _RE_PRICE_BLOCK.search(art_html)
        price_txt = _strip_tags(price_m.group(1)) if price_m else ""
        price = _parse_price_usd(price_txt)

        img_m = _RE_IMG.search(art_html)
        img = (img_m.group(1) if img_m else "") or ""
        if "no_cover.jpg" in img:
            img = ""

        label_m = _RE_LABEL.search(art_html)
        label = _strip_tags(label_m.group(1)) if label_m else ""

        avail_m = _RE_AVAIL.search(art_html)
        availability = _strip_tags(avail_m.group(1)) if avail_m else ""

        atc_m = _RE_ATC.search(art_html)
        add_to_cart = urljoin(BASE, atc_m.group(1)) if atc_m else ""

        vid_m = _RE_ARTICLE_ID.search(art_html)
        variation_id = vid_m.group(1) if vid_m else ""

        out.append(
            {
                "isbn": isbn,
                "title": title,
                "author": author,
                "price_usd": price,
                "detail_url": detail_url,
                "image_url": img,
                "label": label,
                "availability": availability,
                "variation_id": variation_id,
                "add_to_cart": add_to_cart,
            }
        )

    return out

def parse_catalog_page(html: str) -> List[Dict[str, Any]]:
    if HTMLParser is not None:
        return _parse_page_items_selectolax(html)
    return _parse_page_items_regex(html)

def load_seen_urls(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    seen: Set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        t = line.strip()
        if t:
            seen.add(t)
    return seen

def append_seen_urls(path: Path, urls: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for u in urls:
            f.write(u + "\n")

def to_std_row(item: Dict[str, Any], *, site: str) -> Dict[str, Any]:
    isbn = (item.get("isbn") or "").strip()
    titulo = (item.get("title") or "").strip()
    autor = (item.get("author") or "").strip()
    url = (item.get("detail_url") or "").strip()
    portada = (item.get("image_url") or "").strip()

    row: Dict[str, Any] = {
        "ISBN": isbn,
        "TITULO": titulo,
        "AUTOR": autor,
        "EDITORIAL": "",
        "SINOPSIS": "",
        "IDIOMA": "",
        "PAGINAS": "",
        "DIMENSIONES": "",
        "FECHA PUBLICACION": "",
        "URL": url,
        "URL PORTADA": portada,
        "SITE": site,
        "precio_usd": item.get("price_usd"),
        "label": item.get("label"),
        "availability": item.get("availability"),
        "variation_id": item.get("variation_id"),
        "add_to_cart": item.get("add_to_cart"),
    }
    return row

def write_csv(path: Path, rows: List[Dict[str, Any]], *, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "std":
        fields = STD_FIELDS
    else:
        # full: todas las keys que aparezcan
        keys: Set[str] = set()
        for r in rows:
            keys.update(r.keys())
        # prioridad: STD_FIELDS primero
        fields = STD_FIELDS + sorted([k for k in keys if k not in STD_FIELDS])

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

def crawl_catalog(
    *,
    start_page: int,
    end_page: int,
    max_pages: int,
    delay: float,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
    seen_file: Path,
    resume: bool,
    site: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Retorna (rows, last_page_detected)
    """
    rows: List[Dict[str, Any]] = []

    seen = load_seen_urls(seen_file) if resume else set()
    print(f"[INFO] Resume={resume} | Seen URLs={len(seen)} | Seen file={seen_file}")

    with _make_client() as client:
        # descargar page 0 para detectar last_page si hace falta
        first_html = _get_with_retries(
            client,
            _catalog_url(start_page),
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        last_page_detected = _detect_last_page(first_html)

        effective_end = end_page if end_page >= 0 else last_page_detected
        if effective_end < start_page:
            effective_end = start_page

        pages = list(range(start_page, effective_end + 1))
        if max_pages and max_pages > 0:
            pages = pages[:max_pages]

        # procesar start_page con el HTML ya descargado
        print(f"[INFO] Pages: start={start_page} end={effective_end} (detected last={last_page_detected}) total={len(pages)}")
        for idx, page in enumerate(pages):
            if page == start_page:
                html = first_html
            else:
                time.sleep(delay + random.uniform(0, delay * 0.25))
                html = _get_with_retries(
                    client,
                    _catalog_url(page),
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                )

            items = parse_catalog_page(html)
            new_seen: List[str] = []

            print(f"[PAGE {page}] items={len(items)}")
            for it in items:
                url = (it.get("detail_url") or "").strip()
                if not url:
                    continue
                if url in seen:
                    continue

                row = to_std_row(it, site=site)
                rows.append(row)

                seen.add(url)
                new_seen.append(url)

            if resume and new_seen:
                append_seen_urls(seen_file, new_seen)

    return rows, last_page_detected

def maybe_upsert_db(db_path: str, rows: List[Dict[str, Any]]) -> None:
    if connect is None or init_db is None or upsert_many is None:
        print("[WARN] No se pudo importar app.storage.book_std_db. Se omite insert a DB.")
        return
    if not rows:
        print("[INFO] No hay filas nuevas para insertar en DB.")
        return

    con = connect(db_path)
    try:
        init_db(con)
        upsert_many(con, rows)  # ⚠️ inserta STD_FIELDS + raw_json con extras
        con.commit()
        print(f"[OK] DB upsert -> {db_path} | filas={len(rows)}")
    finally:
        con.close()

def main() -> None:
    ap = argparse.ArgumentParser(description="Crawler catálogo BookPeople (/books?page=N)")
    ap.add_argument("--site", default=DEFAULT_SITE, help="SITE para guardar (recomendado: bookpeople_catalog)")
    ap.add_argument("--start-page", type=int, default=0, help="Página inicial (0-index)")
    ap.add_argument("--end-page", type=int, default=-1, help="Página final inclusive (0-index). -1 = auto")
    ap.add_argument("--max-pages", type=int, default=0, help="Limitar cantidad de páginas (0 = sin límite)")
    ap.add_argument("--delay", type=float, default=1.2, help="Delay base entre páginas (seg)")
    ap.add_argument("--timeout", type=float, default=30.0, help="Timeout HTTP (seg)")
    ap.add_argument("--max-retries", type=int, default=3, help="Reintentos HTTP")
    ap.add_argument("--retry-backoff", type=float, default=1.7, help="Backoff base (exponencial)")
    ap.add_argument("--seen-file", default=DEFAULT_SEEN_FILE, help="Archivo para resume (URLs ya procesadas)")
    ap.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True, help="Reanudar desde seen-file")
    ap.add_argument("--out-std", default=DEFAULT_OUT_STD, help="CSV con columnas estándar (STD_FIELDS)")
    ap.add_argument("--out-full", default=DEFAULT_OUT_FULL, help="CSV con columnas estándar + extras")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite DB (book_std)")
    ap.add_argument("--no-db", action="store_true", help="No insertar en DB")

    args = ap.parse_args()

    seen_path = Path(args.seen_file)
    out_std = Path(args.out_std)
    out_full = Path(args.out_full)

    rows, last_detected = crawl_catalog(
        start_page=args.start_page,
        end_page=args.end_page,
        max_pages=args.max_pages,
        delay=args.delay,
        timeout=args.timeout,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        seen_file=seen_path,
        resume=args.resume,
        site=args.site,
    )

    print(f"[INFO] last_page_detected={last_detected} | new_rows={len(rows)}")

    if rows:
        write_csv(out_std, rows, mode="std")
        write_csv(out_full, rows, mode="full")
        print(f"[OK] CSV std  -> {out_std}")
        print(f"[OK] CSV full -> {out_full}")

    if not args.no_db:
        maybe_upsert_db(args.db, rows)

if __name__ == "__main__":
    main()
