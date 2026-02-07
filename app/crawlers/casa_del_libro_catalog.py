from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from app.config import STATE_DIR, DB_PATH

import httpx

from app.storage.book_std_db import Database

# Wrapper estable
from app.sites import casa_del_libro as site

BASE = "https://www.casadellibro.com"
SITE_ID = "casa_del_libro"

# --- URL patterns (producto) ---
_RE_PRODUCT = re.compile(r"/(libro|ebook)-[^/]+/\d{10,13}/\d+$", re.I)
_RE_PRODUCT_LOOSE = re.compile(r"/(libro|ebook)-", re.I)

def _canonical_url(url: str) -> str:
    """Normaliza URL: quita fragment (#...) y ordena parámetros cuando aplica."""
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("#", 1)[0]
    return url

def _category_json_url(category_url: str) -> str:
    """Convierte una URL de categoría a su variante JSON (?json=true)."""
    u = _canonical_url(category_url)
    if not u:
        return u
    return u + ("&" if "?" in u else "?") + "json=true"

def _extract_from_category_json(json_text: str, base_url: str) -> Tuple[Set[str], Set[str], Set[str]]:
    """Devuelve (productos, categorías, paginación) a partir del JSON de categoría."""
    products: Set[str] = set()
    cats: Set[str] = set()
    pages: Set[str] = set()

    try:
        data = json.loads(json_text)
    except Exception:
        return products, cats, pages

    # 1) Productos: buscamos cualquier resolvedComponent con content.products
    def walk(obj: Any):
        if isinstance(obj, dict):
            # productos
            content = obj.get("content")
            if isinstance(content, dict) and isinstance(content.get("products"), list):
                for p in content["products"]:
                    try:
                        link = p.get("link") or {}
                        src = link.get("src") or ""
                        if src:
                            products.add(urljoin(base_url, _canonical_url(src)))
                    except Exception:
                        pass
                # paginación
                pager = content.get("pager") or {}
                if isinstance(pager, dict):
                    # urlsPaginador suele traer /p2#json=true
                    for it in (pager.get("urlsPaginador") or []):
                        if isinstance(it, dict) and it.get("src"):
                            src = _canonical_url(str(it["src"]))
                            if src:
                                pages.add(urljoin(base_url, src))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)

    # regex porque hay links en varios lugares.
    for m in re.findall(r'"/libros/[^"]+"', json_text):
        rel = m.strip('"')
        rel = _canonical_url(rel)
        if not rel:
            continue
        # filtros para tipos #json=true&
        if "?" in rel:
            continue
        if _RE_PRODUCT.search(rel):
            continue
        if _RE_CAT.search(rel):
            cats.add(urljoin(base_url, rel))

    return products, cats, pages

# --- Categoría / listado ---
_RE_CAT = re.compile(r"^/libros(?:/|$)", re.I)

# --- limpieza simple ---
_RE_WS = re.compile(r"\s+")

@dataclass
class CrawlStats:
    discovered: int = 0
    visited: int = 0
    written: int = 0
    skipped_seen: int = 0
    skipped_non_product: int = 0
    skipped_http: int = 0
    errors: int = 0

def _clean(s: Any) -> str:
    if s is None:
        return ""
    return _RE_WS.sub(" ", str(s).replace("\xa0", " ")).strip()

def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def _load_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]

def _append_lines(path: Path, lines: Iterable[str]) -> None:
    _ensure_parent(path)
    with path.open("a", encoding="utf-8", newline="") as f:
        for ln in lines:
            if ln:
                f.write(ln + "\n")

def _is_product_url(url: str) -> bool:
    #obliga devolver un patron exacto
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.netloc and "casadellibro.com" not in p.netloc:
        return False
    path = p.path or ""
    return bool(_RE_PRODUCT.search(path))

def _normalize_url(u: str) -> str:
    u = _clean(u)
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/"):
        u = urljoin(BASE, u)
    return u

def _get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_backoff: float = 1.6,
) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            r = client.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(max(0.2, retry_backoff ** attempt))
                continue
            raise last_exc

def _iter_sitemap_loc_bytes(xml_bytes: bytes) -> Iterator[str]:
    """
    Itera <loc>...</loc> de un XML (sitemap o sitemapindex), sin cargar todo.
    """
    # ET.iterparse necesita file-like
    bio = io.BytesIO(xml_bytes)
    # end events para liberar memoria
    for _, el in ET.iterparse(bio, events=("end",)):
        tag = el.tag.lower()
        if tag.endswith("loc") and el.text:
            yield el.text.strip()
        el.clear()

def _fetch_sitemap_bytes(client: httpx.Client, url: str, *, timeout: float, max_retries: int, retry_backoff: float) -> bytes:
    r = _get_with_retries(client, url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
    data = r.content or b""
    # gzip por header o por extensión
    ct = (r.headers.get("content-type") or "").lower()
    if url.lower().endswith(".gz") or "gzip" in ct:
        try:
            return gzip.decompress(data)
        except Exception:
            # a veces ya viene descomprimido
            return data
    return data

def _robots_sitemaps(client: httpx.Client, *, timeout: float, max_retries: int, retry_backoff: float) -> List[str]:
    """Extrae URLs de sitemap desde robots.txt (Sitemap: ...)."""
    try:
        r = _get_with_retries(client, f"{BASE}/robots.txt", timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
        text = r.text or ""
    except Exception:
        return []
    out: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("sitemap:"):
            sm = line.split(":", 1)[1].strip()
            sm = _normalize_url(sm)
            if sm:
                out.append(sm)
    # dedupe manteniendo orden
    dedup: List[str] = []
    seen: Set[str] = set()
    for u in out:
        if u not in seen:
            dedup.append(u)
            seen.add(u)
    return dedup

def discover_sitemaps(client: httpx.Client, *, timeout: float, max_retries: int, retry_backoff: float) -> List[str]:
    """Descubre sitemaps probables (best-effort).

    Prioridad:
    1) robots.txt (si declara Sitemap:)
    2) rutas comunes (/sitemap.xml, /sitemap_index.xml, etc.)
    """
    candidates: List[str] = []
    candidates.extend(_robots_sitemaps(client, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff))
    candidates.extend(
        [
            f"{BASE}/sitemap.xml",
            f"{BASE}/sitemap.xml.gz",
            f"{BASE}/sitemap_index.xml",
            f"{BASE}/sitemap-index.xml",
            f"{BASE}/sitemapindex.xml",
        ]
    )

    # dedupe manteniendo orden
    cand2: List[str] = []
    seen_c: Set[str] = set()
    for u in candidates:
        uu = _normalize_url(u)
        if uu and uu not in seen_c:
            cand2.append(uu)
            seen_c.add(uu)

    sitemaps: List[str] = []
    for u in cand2:
        try:
            xml = _fetch_sitemap_bytes(client, u, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
        except Exception:
            continue

        locs = list(_iter_sitemap_loc_bytes(xml))
        if not locs:
            continue

        # Si parece un index (locs con .xml/.gz), lo expandimos.
        xmlish = [loc for loc in locs if str(loc).lower().endswith((".xml", ".xml.gz", ".gz"))]
        if xmlish:
            sitemaps.extend([_normalize_url(x) for x in xmlish if _normalize_url(x)])
            # también agregamos el index por si trae urlset directo
            sitemaps.append(u)
        else:
            # urlset "final"
            sitemaps.append(u)

        # con uno que funcione alcanza
        if sitemaps:
            break

    # dedupe manteniendo orden
    out: List[str] = []
    seen: Set[str] = set()
    for u in sitemaps:
        uu = _normalize_url(u)
        if uu and uu not in seen:
            out.append(uu)
            seen.add(uu)
    return out

def iter_product_urls_from_sitemaps(
    client: httpx.Client,
    sitemap_urls: List[str],
    *,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
    limit: int,
) -> Iterator[str]:
    """
    Genera URLs de producto leyendo uno o varios sitemaps.
    """
    seen: Set[str] = set()
    count = 0

    for sm_url in sitemap_urls:
        if limit and count >= limit:
            return
        try:
            xml = _fetch_sitemap_bytes(client, sm_url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
        except Exception:
            continue

        for loc in _iter_sitemap_loc_bytes(xml):
            if limit and count >= limit:
                return
            u = _normalize_url(loc)
            if not u or u in seen:
                continue
            seen.add(u)
            if _is_product_url(u):
                yield u
                count += 1

def _extract_links(html: str) -> List[str]:
    """
    Extrae hrefs rápido con regex.
    (No dependemos de bs4 para mantener el crawler liviano.)
    """
    # href="..."
    hrefs = re.findall(r'href\s*=\s*"([^"]+)"', html, flags=re.I)
    # href='...'
    hrefs += re.findall(r"href\s*=\s*'([^']+)'", html, flags=re.I)
    out: List[str] = []
    for h in hrefs:
        u = _normalize_url(h)
        if u:
            out.append(u)
    return out

def fetch_html(
    client: httpx.Client,
    url: str,
    *,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_backoff: float = 1.6,
) -> str:
    """Fetch HTML (fallback del modo category)."""
    r = _get_with_retries(
        client,
        url,
        timeout=timeout,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
    )
    return r.text or ""

def extract_product_urls_from_html(html: str) -> List[str]:
    """Extrae URLs relativas de productos desde HTML de listado."""
    urls: Set[str] = set()

    # Ruta típica: /libro-<slug>/<isbn>/<id> o /ebook-...
    for m in re.findall(r'/(?:libro|ebook)-[^"\'\s<>]+/\d{10,13}/\d+', html, flags=re.I):
        urls.add(m)

    # Fallback (por si el HTML viene raro)
    if not urls:
        for u in _extract_links(html):
            try:
                p = urlparse(u)
                if _RE_PRODUCT.search(p.path or ""):
                    urls.add(p.path)
            except Exception:
                pass

    return sorted(urls)

def extract_category_urls_from_html(html: str) -> List[str]:
    """Extrae URLs relativas de categorías/listados desde HTML."""
    urls: Set[str] = set()

    for u in _extract_links(html):
        try:
            p = urlparse(u)
        except Exception:
            continue

        path = p.path or ""
        if not path:
            continue

        # evitar productos
        if _RE_PRODUCT.search(path):
            continue

        low = path.lower()
        if not (low.startswith("/libros") or low.startswith("/ebooks")):
            continue

        # descartar páginas de autor/serie, etc.
        if any(x in low for x in ("/libros-ebooks/", "/autor/", "/autores/", "/serie-saga/")):
            continue

        rel = path
        if p.query:
            rel = rel + "?" + p.query

        urls.add(rel)

    return sorted(urls)

def iter_product_urls_from_category(
    client: httpx.Client,
    seed_url: str,
    *,
    limit: int = 1000,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_backoff: float = 1.6,
    delay: float = 0.1,
) -> Iterator[str]:
    """Recorre categorías partiendo de `seed_url` y devuelve URLs de producto.

    Preferimos la variante `?json=true` (más estable y rápida) y caemos a HTML
    sólo si el JSON no está disponible.
    """
    # Internals (el caller pasa limit/timeout/retries/delay)
    seen_pages: Set[str] = set()
    max_urls = int(limit) if limit and int(limit) > 0 else 10**18
    delay_s = max(0.0, float(delay))

    q: List[str] = []
    start = _canonical_url(_normalize_url(seed_url))
    if start:
        q.append(start)

    emitted: int = 0

    while q and emitted < max_urls:
        page_url = _canonical_url(q.pop(0))
        if not page_url or page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        # 1) Intento JSON
        try:
            json_url = _category_json_url(page_url)
            rj = _get_with_retries(client, json_url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
            txtj = (rj.text or "").strip()
            if txtj.startswith("{"):
                prods, cats, pages = _extract_from_category_json(txtj, BASE)
                for u in pages:
                    u2 = _canonical_url(_normalize_url(u))
                    if u2 and u2 not in seen_pages:
                        q.append(u2)
                for u in cats:
                    u2 = _canonical_url(_normalize_url(u))
                    if u2 and u2 not in seen_pages:
                        q.append(u2)

                for u in prods:
                    u2 = _canonical_url(_normalize_url(u))
                    if not u2:
                        continue
                    if not _RE_PRODUCT.search(u2):
                        continue
                    yield u2
                    emitted += 1
                    if emitted >= max_urls:
                        break

                if emitted >= max_urls:
                    break

                # Si el JSON trajo algo, evitamos el fetch HTML
                if prods or cats or pages:
                    time.sleep(delay_s)
                    continue
        except Exception:
            pass

        # 2) Fallback HTML (más lento)
        try:
            html = fetch_html(client, page_url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
        except Exception:
            time.sleep(delay_s)
            continue

        for p in extract_product_urls_from_html(html):
            u = _canonical_url(_normalize_url(urljoin(BASE, p)))
            if not u:
                continue
            yield u
            emitted += 1
            if emitted >= max_urls:
                break

        if emitted >= max_urls:
            break

        for c in extract_category_urls_from_html(html):
            u = _canonical_url(_normalize_url(urljoin(BASE, c)))
            if u and u not in seen_pages:
                q.append(u)

        time.sleep(delay_s)

def crawl(
    *,
    mode: str,
    seed: str,
    max_urls: int,
    delay: float,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
    seen_file: str,
    fail_file: str,
    write_db: bool,
    db_path: str,
    quiet: bool,
) -> Dict[str, Any]:
    """
    Devuelve dict con stats y algunos metadatos.
    """
    stats = CrawlStats()

    seen_path = Path(seen_file) if seen_file else None
    fail_path = Path(fail_file) if fail_file else None
    seen_urls: Set[str] = set(_load_lines(seen_path)) if seen_path else set()

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BookSearchV2/1.0; +https://example.invalid)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    }

    with httpx.Client(headers=headers) as client:
        # 1) fuente de URLs
        if mode == "sitemap":
            sitemaps = discover_sitemaps(client, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
            if not sitemaps and not quiet:
                print("[WARN] No pude descubrir sitemaps automáticamente. Probá --mode category con --seed.")
            url_iter = iter_product_urls_from_sitemaps(
                client, sitemaps, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff, limit=max_urls
            )
        else:
            url_iter = iter_product_urls_from_category(
                client,
                seed,
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                limit=max_urls,
                delay=delay,
            )

        # 2) DB
        db = Database(db_path) if write_db else None
        batch: List[Dict[str, Any]] = []
        batch_size = 50

        try:
            for url in url_iter:
                url = _normalize_url(url)
                if not url:
                    continue
                stats.discovered += 1

                if url in seen_urls:
                    stats.skipped_seen += 1
                    continue
                if not _is_product_url(url):
                    stats.skipped_non_product += 1
                    continue

                stats.visited += 1
                try:
                    row = site.product(url)  # raw_api OFF por defecto (por wrapper)
                except Exception:
                    row = None
                    stats.errors += 1
                    if fail_path:
                        _append_lines(fail_path, [url])
                    continue

                if not isinstance(row, dict):
                    stats.skipped_http += 1
                    if fail_path:
                        _append_lines(fail_path, [url])
                    continue
                # Asegurar compat DB (book_std_db espera key 'sitio')
                if isinstance(row, dict):
                    row.setdefault("sitio", SITE_ID)
                    row.setdefault("url_detalle", url)

                batch.append(row)
                seen_urls.add(url)
                if seen_path:
                    _append_lines(seen_path, [url])

                # flush
                if db and len(batch) >= batch_size:
                    inserted, updated = db.upsert_many(batch)
                    stats.written += inserted + updated
                    batch.clear()

                if delay:
                    time.sleep(delay)

            # flush final
            if db and batch:
                inserted, updated = db.upsert_many(batch)
                stats.written += inserted + updated
                batch.clear()

        finally:
            if db:
                db.close()

    out = {
        "site": SITE_ID,
        "mode": mode,
        "seed": seed,
        "db_path": db_path if write_db else "",
        "write_db": write_db,
        "stats": {
            "discovered": stats.discovered,
            "visited": stats.visited,
            "written": stats.written,
            "skipped_seen": stats.skipped_seen,
            "skipped_non_product": stats.skipped_non_product,
            "skipped_http": stats.skipped_http,
            "errors": stats.errors,
        },
        "seen_file": str(seen_path) if seen_path else "",
        "fail_file": str(fail_path) if fail_path else "",
    }
    return out

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crawler de catálogo: Casa del Libro")
    p.add_argument("--mode", choices=["sitemap", "category"], default="sitemap", help="Fuente de URLs.")
    p.add_argument("--seed", default=f"{BASE}/libros", help="Seed para --mode category.")
    p.add_argument("--max-urls", type=int, default=0, help="0 = sin límite (ojo: enorme).")
    p.add_argument("--delay", type=float, default=0.2, help="Pausa entre fichas (seg).")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--retry-backoff", type=float, default=1.6)
    p.add_argument("--seen-file", default=str(STATE_DIR / "casa_del_libro_seen_urls.txt"))
    p.add_argument("--fail-file", default=str(STATE_DIR / "casa_del_libro_fail_urls.txt"))
    p.add_argument("--write-db", action="store_true", help="Upsert a la DB estándar.")
    p.add_argument("--db-path", default=str(DB_PATH))
    p.add_argument("--quiet", action="store_true")
    return p

def main() -> None:
    args = build_argparser().parse_args()

    max_urls = int(args.max_urls or 0)
    out = crawl(
        mode=args.mode,
        seed=args.seed,
        max_urls=max_urls,
        delay=float(args.delay),
        timeout=float(args.timeout),
        max_retries=int(args.max_retries),
        retry_backoff=float(args.retry_backoff),
        seen_file=str(args.seen_file),
        fail_file=str(args.fail_file),
        write_db=bool(args.write_db),
        db_path=str(args.db_path),
        quiet=bool(args.quiet),
    )

    import json
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
