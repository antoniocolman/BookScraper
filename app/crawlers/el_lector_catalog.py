# app/crawlers/el_lector_catalog.py
from __future__ import annotations

import csv
import time
import re
from pathlib import Path
from typing import Dict, Any, Iterable, List, Optional, Set
from urllib.parse import urljoin

import httpx

from app.sites import el_lector
from app.standard import STD_FIELDS, to_standard_row
from app.storage.book_std_db import connect, init_db, upsert_many

BASE = "https://www.ellector.com.py"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# SOLO productos reales (con -id-....html) y evitando categorías
_RE_PROD = re.compile(
    r'href=["\'](/productos/(?!categoria-)(?!categorias/)[^"\']*?-id-[^"\']+?\.html)["\']',
    re.I,
)

# detectar "productos-pagina-2499.html"
_RE_PAGE = re.compile(r"productos-pagina-(\d+)\.html", re.I)


def catalog_page_url(page: int) -> str:
    if page <= 1:
        return f"{BASE}/productos.html"
    return f"{BASE}/productos-pagina-{page}.html"


def extract_product_urls(html: str) -> List[str]:
    rels = _RE_PROD.findall(html or "")
    urls = [urljoin(BASE, r) for r in rels]
    # unique keep order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def detect_last_page(html: str) -> Optional[int]:
    nums = [int(x) for x in _RE_PAGE.findall(html or "")]
    return max(nums) if nums else None


def load_seen(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    return set(
        p.strip()
        for p in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if p.strip()
    )


def mark_seen(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(url + "\n")


def append_standard_csv(out_path: Path, std_rows: Iterable[Dict[str, Any]]) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = out_path.exists() and out_path.stat().st_size > 0

    n = 0
    with out_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STD_FIELDS, extrasaction="ignore")
        if not file_exists:
            w.writeheader()
        for r in std_rows:
            w.writerow(r)
            n += 1
    return n


def _append_fail(fail_file: Optional[Path], url: str, reason: str) -> None:
    if not fail_file:
        return
    fail_file.parent.mkdir(parents=True, exist_ok=True)
    with fail_file.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{url}\t{reason}\n")


def _get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
) -> httpx.Response:
    """
    Reintenta ante errores de red/timeout. Backoff exponencial suave.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            return client.get(url, timeout=timeout)
        except BaseException as e:
            last_exc = e
            if attempt >= max_retries:
                break
            sleep_s = min(30.0, (retry_backoff ** attempt))
            time.sleep(sleep_s)
    raise last_exc or RuntimeError("Unknown error in _get_with_retries")


def _autodetect_end_page(
    client: httpx.Client,
    *,
    start_page: int,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
    verbose: bool,
) -> int:
    """
    1) Intenta detectar por regex en el HTML.
    2) Si no hay pistas, hace búsqueda exponencial + binaria:
       - encuentra un 'last_good' con productos
       - encuentra un 'first_bad' sin productos/404
       - binaria para el último válido
    """
    first_url = catalog_page_url(start_page)
    r1 = _get_with_retries(client, first_url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
    if r1.status_code != 200:
        return start_page
    r1.encoding = "utf-8"
    last = detect_last_page(r1.text)
    if last:
        return last

    def page_ok(p: int) -> bool:
        url = catalog_page_url(p)
        r = _get_with_retries(client, url, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff)
        if r.status_code != 200:
            return False
        r.encoding = "utf-8"
        return len(extract_product_urls(r.text)) > 0

    # Exponencial
    last_good = start_page
    step = 1
    while True:
        candidate = last_good + step
        if candidate > 50000:  # safety cap
            break
        if page_ok(candidate):
            last_good = candidate
            step *= 2
            if verbose:
                print(f"[CRAWL] autodetect probe ok -> {candidate}")
            continue
        first_bad = candidate
        if verbose:
            print(f"[CRAWL] autodetect probe bad -> {candidate}")
        break
    else:
        first_bad = last_good + 1

    # Si nunca encontró un bad, devolvemos last_good
    if "first_bad" not in locals():
        return last_good

    # Binaria entre (last_good, first_bad-1)
    lo = last_good
    hi = first_bad - 1
    best = last_good
    while lo <= hi:
        mid = (lo + hi) // 2
        if page_ok(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def crawl(
    *,
    start_page: int = 1,
    end_page: int = 0,  # 0=autodetect
    delay: float = 1.0,
    timeout: float = 25.0,
    out_csv: Path,
    seen_file: Path,
    only_with_isbn: bool = True,
    max_products: int = 0,  # 0=sin límite (se aplica sobre written_total)
    verbose: bool = True,
    write_db: bool = False,
    db_path: str = r".\data\booksearchv2.db",
    flush_every: int = 50,
    max_retries: int = 2,
    retry_backoff: float = 1.7,
    fail_file: Optional[Path] = None,
    max_empty_pages: int = 2,  # si encuentra N páginas seguidas sin productos: corta
) -> Dict[str, Any]:
    headers = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}

    seen = load_seen(seen_file)
    written_total = 0
    visited_total = 0

    # métricas
    skipped_seen = 0
    skipped_no_isbn = 0
    skipped_http = 0
    errors = 0
    empty_pages_streak = 0

    # DB opcional
    con = None
    if write_db:
        con = connect(db_path)
        init_db(con)

    def flush(buffer_rows: List[Dict[str, Any]], buffer_urls: List[str]) -> None:
        nonlocal written_total
        if not buffer_rows:
            return

        written = append_standard_csv(out_csv, buffer_rows)
        written_total += written
        if verbose:
            print(f"  [CSV] +{written} (total={written_total})")

        if con is not None:
            ins, upd = upsert_many(con, buffer_rows)
            if verbose:
                print(f"  [DB] +ins={ins} +upd={upd}")

        # marcamos seen recién cuando persistimos
        for u in buffer_urls:
            if u not in seen:
                seen.add(u)
                mark_seen(seen_file, u)

        buffer_rows.clear()
        buffer_urls.clear()

    try:
        with httpx.Client(headers=headers, follow_redirects=True) as client:
            if end_page == 0:
                end_page = _autodetect_end_page(
                    client,
                    start_page=start_page,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                    verbose=verbose,
                )
                if verbose:
                    print(f"[CRAWL] end-page autodetect -> {end_page}")

            stop_all = False

            for page in range(start_page, end_page + 1):
                page_url = catalog_page_url(page)
                if verbose:
                    print(f"\n[PAGE {page}] {page_url}")

                try:
                    r = _get_with_retries(
                        client,
                        page_url,
                        timeout=timeout,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff,
                    )
                except BaseException as e:
                    errors += 1
                    _append_fail(fail_file, page_url, f"catalog_fetch_error: {e}")
                    if verbose:
                        print(f"  [ERR] catálogo fetch -> {e}")
                    continue

                if r.status_code != 200:
                    if verbose:
                        print(f"  [WARN] status={r.status_code} -> salto página")
                    continue

                r.encoding = "utf-8"
                prod_urls = extract_product_urls(r.text)

                if verbose:
                    print(f"  [FOUND] {len(prod_urls)} urls producto")

                if len(prod_urls) == 0:
                    empty_pages_streak += 1
                    if empty_pages_streak >= max_empty_pages:
                        if verbose:
                            print(f"[STOP] {empty_pages_streak} páginas seguidas vacías. Corto.")
                        break
                else:
                    empty_pages_streak = 0

                buffer_rows: List[Dict[str, Any]] = []
                buffer_urls: List[str] = []

                for u in prod_urls:
                    if u in seen:
                        skipped_seen += 1
                        continue

                    if delay:
                        time.sleep(delay)

                    visited_total += 1

                    try:
                        pr = _get_with_retries(
                            client,
                            u,
                            timeout=timeout,
                            max_retries=max_retries,
                            retry_backoff=retry_backoff,
                        )
                        if pr.status_code != 200:
                            skipped_http += 1
                            seen.add(u)
                            mark_seen(seen_file, u)
                            _append_fail(fail_file, u, f"http_status={pr.status_code}")
                            continue

                        pr.encoding = "utf-8"

                        raw = el_lector._parse_product_page(pr.text, u)

                        if only_with_isbn and not raw.get("isbn"):
                            skipped_no_isbn += 1
                            seen.add(u)
                            mark_seen(seen_file, u)
                            continue

                        std = to_standard_row(raw, site_id=el_lector.SITE_ID)
                        buffer_rows.append(std)
                        buffer_urls.append(u)

                        if len(buffer_rows) >= flush_every:
                            flush(buffer_rows, buffer_urls)

                        if max_products and (written_total + len(buffer_rows)) >= max_products:
                            flush(buffer_rows, buffer_urls)
                            if verbose:
                                print("[STOP] max-products alcanzado")
                            stop_all = True
                            break

                    except BaseException as e:
                        errors += 1
                        seen.add(u)
                        mark_seen(seen_file, u)
                        _append_fail(fail_file, u, f"product_error: {e}")
                        if verbose:
                            print(f"  [ERR] {u} -> {e}")

                flush(buffer_rows, buffer_urls)

                if stop_all:
                    break

    finally:
        if con is not None:
            con.close()

    return {
        "written": written_total,
        "visited": visited_total,
        "out_csv": str(out_csv),
        "seen_file": str(seen_file),
        "start_page": start_page,
        "end_page": end_page,
        "db_path": db_path if write_db else "",
        "write_db": write_db,
        "skipped_seen": skipped_seen,
        "skipped_no_isbn": skipped_no_isbn,
        "skipped_http": skipped_http,
        "errors": errors,
    }
