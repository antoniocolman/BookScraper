from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from app.sites.experimental import yenny_core as core
from app.storage.book_std_db import connect, init_db, upsert_many

from app.config import DB_PATH, STATE_DIR


STD_HEADER = [
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

BASE = "https://www.yenny-elateneo.com"
SITE = "yenny"


@dataclass
class CrawlStats:
    visited: int = 0
    written: int = 0
    skipped_seen: int = 0
    skipped_no_isbn: int = 0
    skipped_http: int = 0
    errors: int = 0


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]


def _load_seen(seen_file: Path) -> set[str]:
    return set(_load_lines(seen_file))


def _append_lines(path: Path, lines: Iterable[str]) -> None:
    _ensure_parent(path)
    with path.open("a", encoding="utf-8", newline="") as f:
        for ln in lines:
            if ln:
                f.write(ln + "\n")


def _fmt_pages(v: Any) -> str:
    """
    Objetivo: '224 págs' (string), útil para web.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    low = s.lower()
    if "pág" in low or "pag" in low:
        return s
    if s.isdigit():
        return f"{s} págs"
    return s


def _clean_cover(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    low = u.lower()
    if "empty-placeholder.png" in low:
        return ""
    return u


def _pick(parsed: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = parsed.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _std_row(parsed: Dict[str, Any], url: str) -> Dict[str, str]:
    # Soportamos varios nombres por compatibilidad
    isbn = _pick(parsed, "isbn", "ISBN", "isbn13", "gtin13")
    titulo = _pick(parsed, "title", "titulo", "name")
    autor = _pick(parsed, "author", "autor")
    editorial = _pick(parsed, "editorial", "publisher")
    sinopsis = _pick(parsed, "sinopsis", "descripcion", "description")
    idioma = _pick(parsed, "idioma", "language")
    paginas = _fmt_pages(parsed.get("paginas") or parsed.get("pages"))
    dimensiones = _pick(parsed, "dimensiones", "dimensions")
    fecha_pub = _pick(parsed, "fecha_publicacion", "fecha", "published", "publication_date")
    portada = _clean_cover(_pick(parsed, "portada_url", "cover", "image", "imagen", "url_portada"))

    return {
        "ISBN": isbn,
        "TITULO": titulo,
        "AUTOR": autor,
        "EDITORIAL": editorial,
        "SINOPSIS": sinopsis,
        "IDIOMA": idioma,
        "PAGINAS": paginas,
        "DIMENSIONES": dimensiones,
        "FECHA PUBLICACION": fecha_pub,
        "URL": url,
        "URL PORTADA": portada,
        "SITE": SITE,
    }


def _get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    max_retries: int = 3,
    retry_backoff: float = 1.5,
) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            r = client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            # backoff simple
            if attempt < max_retries:
                time.sleep(max(0.2, retry_backoff ** attempt))
                continue
            raise
    # never reached
    raise last_exc or RuntimeError("Unknown error")


def _xhr_page_url(page: int, *, limit: int, theme: str, query: str, sort_by: str) -> str:
    # Endpoint capturado por vos: /search/page/N/?q=&results_only=true&limit=12&theme=toluca
    # Probamos incluir sort_by; si el backend lo ignora, no pasa nada.
    q = query or ""
    theme = theme or "toluca"
    limit = int(limit or 12)
    sort_by = sort_by or ""
    tail = f"{BASE}/search/page/{page}/?q={httpx.QueryParams({'q': q})['q']}&results_only=true&limit={limit}&theme={theme}"
    if sort_by:
        tail += f"&sort_by={sort_by}"
    return tail


def _productos_page_url(page: int, *, sort_by: str) -> str:
    # Fallback HTML: /productos/?sort_by=alpha-ascending&page=N
    sort_by = sort_by or "alpha-ascending"
    if page <= 1:
        return f"{BASE}/productos/?sort_by={sort_by}"
    return f"{BASE}/productos/?sort_by={sort_by}&page={page}"


def _fetch_listing_page(
    client: httpx.Client,
    *,
    page: int,
    endpoint: str,
    limit: int,
    theme: str,
    query: str,
    sort_by: str,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
) -> Tuple[List[str], bool, str]:
    endpoint = (endpoint or "search").strip().lower()

    if endpoint == "productos":
        url = _productos_page_url(page, sort_by=sort_by)
        r = _get_with_retries(client, url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
        html = r.text
        urls = core.extract_product_urls_from_listing(html, base_url=BASE)
        # autodetect "has_next" intentando detectar paginación, si no, asumimos True hasta que no haya urls nuevas
        has_next = True
        return urls, has_next, url

    # default: search JSON results_only
    url = _xhr_page_url(page, limit=limit, theme=theme, query=query, sort_by=sort_by)
    headers = {
        "accept": "application/json,*/*;q=0.1",
        "x-requested-with": "XMLHttpRequest",
        "referer": f"{BASE}/search/?q={query or ''}&mpage={max(0, page-1)}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BookSearchV2/1.0",
    }
    r = _get_with_retries(client, url, headers=headers, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
    j = r.json()
    html = (j.get("html") or "")
    has_next = bool(j.get("has_next", False))
    urls = core.extract_product_urls_from_listing(html, base_url=BASE)
    return urls, has_next, url


def _fetch_product_html(
    client: httpx.Client,
    url: str,
    *,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
) -> str:
    r = _get_with_retries(client, url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
    return r.text


def crawl(
    *,
    start_page: int,
    end_page: int,  # 0 = autodetect
    output_csv: str,
    delay: float = 0.2,
    timeout: float = 20.0,
    only_with_isbn: bool = True,
    max_products: int = 0,  # 0 = sin límite
    flush_every: int = 50,
    seen_file: Optional[str] = None,
    reset_seen: bool = False,
    write_db: bool = False,
    db_path: str = str(DB_PATH),
    quiet: bool = False,
    # NUEVOS: para que main.py no rompa
    limit: int = 12,
    theme: str = "toluca",
    query: str = "",
    sort_by: str = "alpha-ascending",
    endpoint: str = "search",  # search | productos
    max_retries: int = 3,
    retry_backoff: float = 1.5,
    fail_file: Optional[str] = None,
) -> Dict[str, Any]:
    out_path = Path(output_csv)
    _ensure_parent(out_path)

    if seen_file:
        seen_path = Path(seen_file)
    else:
        seen_path = STATE_DIR / "yenny_seen_urls.txt"

    if fail_file:
        fail_path = Path(fail_file)
    else:
        fail_path = STATE_DIR / "yenny_failed_urls.txt"

    if reset_seen:
        _ensure_parent(seen_path)
        seen_path.write_text("", encoding="utf-8")

    seen = _load_seen(seen_path)

    stats = CrawlStats()

    con = None
    if write_db:
        con = connect(db_path)
        init_db(con)

    file_exists = out_path.exists() and out_path.stat().st_size > 0
    csv_f = out_path.open("a", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(csv_f, fieldnames=STD_HEADER)
    if not file_exists:
        writer.writeheader()

    buffer_rows: List[Dict[str, str]] = []
    buffer_seen_append: List[str] = []
    buffer_fail_append: List[str] = []

    def _flush() -> None:
        nonlocal buffer_rows, buffer_seen_append, buffer_fail_append
        if buffer_rows:
            writer.writerows(buffer_rows)
            csv_f.flush()
            if write_db and con is not None:
                ins, upd = upsert_many(con, buffer_rows)
                if not quiet:
                    print(f"  [DB] +ins={ins} +upd={upd}")
            stats.written += len(buffer_rows)
            buffer_rows = []
        if buffer_seen_append:
            _append_lines(seen_path, buffer_seen_append)
            buffer_seen_append = []
        if buffer_fail_append:
            _append_lines(fail_path, buffer_fail_append)
            buffer_fail_append = []

    with httpx.Client() as client:
        page = int(start_page or 1)
        keep_going = True

        # Para endpoint=productos, "has_next" puede ser ambiguo.
        # Cortamos si en una página no aparecen URLs nuevas (todas ya vistas) o si lista vacía.
        while keep_going:
            if end_page and page > end_page:
                break

            try:
                urls, has_next, listing_url = _fetch_listing_page(
                    client,
                    page=page,
                    endpoint=endpoint,
                    limit=limit,
                    theme=theme,
                    query=query,
                    sort_by=sort_by,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                )
            except Exception as e:
                stats.errors += 1
                if not quiet:
                    print(f"[ERROR] listing page {page}: {e}")
                break

            if not quiet:
                print(f"\n[PAGE {page}] {listing_url}")
                print(f"  [FOUND] {len(urls)} urls producto")

            if not urls:
                # sin resultados -> fin
                break

            # si todas ya fueron vistas, probablemente fin en modo productos
            new_urls = [u for u in urls if u not in seen]
            if endpoint.strip().lower() == "productos" and not new_urls:
                if not quiet:
                    print("  [INFO] Sin URLs nuevas (todas vistas). Cortando.")
                break

            for u in urls:
                stats.visited += 1

                if u in seen:
                    stats.skipped_seen += 1
                    continue

                try:
                    html = _fetch_product_html(
                        client,
                        u,
                        timeout=timeout,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff,
                    )
                    parsed = core.parse_product_page(html, u)
                    row = _std_row(parsed, u)

                    if only_with_isbn and not row["ISBN"]:
                        stats.skipped_no_isbn += 1
                    else:
                        buffer_rows.append(row)

                    seen.add(u)
                    buffer_seen_append.append(u)

                except httpx.HTTPError:
                    stats.skipped_http += 1
                    buffer_fail_append.append(u)
                except Exception as e:
                    stats.errors += 1
                    buffer_fail_append.append(u)
                    if not quiet:
                        print(f"  [ERROR] parse {u}: {e}")

                if max_products and stats.written + len(buffer_rows) >= max_products:
                    _flush()
                    keep_going = False
                    break

                if delay:
                    time.sleep(delay)

                if flush_every and len(buffer_rows) >= flush_every:
                    if not quiet:
                        print(f"  [CSV] +{len(buffer_rows)} (flush)")
                    _flush()

            _flush()

            if end_page == 0:
                keep_going = bool(has_next) if endpoint.strip().lower() == "search" else True
            else:
                keep_going = True

            page += 1

    csv_f.close()
    if con is not None:
        con.close()

    report = {
        "visited": stats.visited,
        "written": stats.written,
        "skipped_seen": stats.skipped_seen,
        "skipped_no_isbn": stats.skipped_no_isbn,
        "skipped_http": stats.skipped_http,
        "errors": stats.errors,
        "out_csv": str(out_path),
        "seen_file": str(seen_path),
        "fail_file": str(fail_path),
        "db_path": str(Path(db_path).resolve()) if write_db else "",
    }
    return report