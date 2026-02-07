# app/sites/amazon_books.py
from __future__ import annotations

from pathlib import Path
from app.config import EXPORTS_DIR
from typing import Any, List, Optional
import random
import threading
import time

SITE_ID = "amazon_books"
SITE_NAME = "Amazon Books (best-effort)"
DEFAULT_OUTPUT = str(EXPORTS_DIR / "amazon_books_resultados.csv")

# Throttle global (para que si el motor usa el site varias veces, comparta ritmo)
_LOCK = threading.Lock()
_LAST_CALL_AT = 0.0
_CALLS_SINCE_PAUSE = 0


def _exp():
    # Import lazy para no romper imports si no usás Amazon
    from .experimental import amazon_books_experimental as ax
    return ax


def _throttle(*, query_delay: float, batch_size: int, batch_pause: float) -> None:
    """
    - query_delay: segundos mínimos entre queries "principales" (por ISBN)
    - batch_size/batch_pause: cada N queries, mete una pausa grande
    """
    global _LAST_CALL_AT, _CALLS_SINCE_PAUSE

    with _LOCK:
        now = time.time()

        if _LAST_CALL_AT and query_delay > 0:
            wait = query_delay - (now - _LAST_CALL_AT)
            if wait > 0:
                time.sleep(wait)

        _CALLS_SINCE_PAUSE += 1

        if batch_size > 0 and batch_pause > 0 and _CALLS_SINCE_PAUSE >= batch_size:
            jitter = random.uniform(0, batch_pause * 0.20)
            sleep_s = batch_pause + jitter
            print(f"[INFO][Amazon] Throttle: {batch_size} consultas -> durmiendo {sleep_s:.1f}s...")
            time.sleep(sleep_s)
            _CALLS_SINCE_PAUSE = 0

        _LAST_CALL_AT = time.time()


def _isbn13_to_isbn10(isbn13: str) -> str:
    """
    Convierte ISBN13 -> ISBN10 SOLO si empieza con 978 y es válido.
    Si no aplica, retorna "".
    """
    s = (isbn13 or "").strip()
    s = "".join(ch for ch in s if ch.isdigit() or ch.upper() == "X")
    if len(s) != 13 or not s.startswith("978") or not s.isdigit():
        return ""

    core = s[3:12]  # 9 dígitos
    total = 0
    for i, ch in enumerate(core, start=1):  # i=1..9
        total += i * int(ch)
    check = total % 11
    check_ch = "X" if check == 10 else str(check)
    return core + check_ch


def _direct_dp_url(isbn10: str) -> str:
    # URL canónica sin parámetros
    return f"https://www.amazon.com/dp/{isbn10}"


def _try_direct_dp(ax, session, isbn13: str, isbn10: str, *, max_retries: int, base_wait: float) -> Optional[dict]:
    """
    Intento "directo" al print book (DP/ISBN10). No evita bloqueos, pero:
    - evita caer en ASIN tipo B... (Kindle/Audible)
    - usa URL canónica sin query params
    """
    if not isbn10:
        return None

    url = _direct_dp_url(isbn10)
    try:
        # usamos helpers del experimental (aunque sean _privados)
        html = ax._fetch_html(session, url, max_retries=max_retries, base_wait=base_wait)
        row = ax.parse_product_page(html, url, isbn_hint=isbn13 or isbn10)

        if not (row.get("TITULO") or "").strip():
            return None
        return row
    except ax.BlockedByAmazon as e:
        print(f"[BLOCKED][Amazon] Direct DP también bloqueado: {e}")
        return None
    except Exception as e:
        print(f"[ERROR][Amazon] Direct DP falló ({url}): {e}")
        return None


def run_single(
    query: str,
    *,
    delay: float = 1.0,          # delay entre search y product (interno)
    query_delay: float = 10.0,   # delay mínimo entre ISBNs (externo)
    batch_size: int = 5,
    batch_pause: float = 120.0,
    max_retries: int = 2,
    base_wait: float = 12.0,
    **_kwargs: Any,
) -> Optional[dict]:
    """
    Ejecuta una consulta única (ISBN) y devuelve dict estándar.
    Retorna None si no encuentra o si hay bloqueo/errores.
    """
    ax = _exp()
    q = ax.parse_query_line(query)
    if not q:
        print(f"[WARN] Consulta inválida: {query!r}")
        return None

    # Derivar ISBN10 para fallback directo
    isbn13 = q.isbn if len(q.isbn) == 13 else ""
    isbn10 = q.isbn if len(q.isbn) == 10 else _isbn13_to_isbn10(isbn13)

    _throttle(query_delay=query_delay, batch_size=batch_size, batch_pause=batch_pause)

    session = ax.make_session()
    try:
        # 1) Camino normal
        try:
            row = ax.search_amazon_books(
                session,
                q,
                delay=delay,
                max_retries=max_retries,
                base_wait=base_wait,
            )
            if row:
                return row
        except ax.BlockedByAmazon as e:
            print(f"[BLOCKED][Amazon] {e}")
        except Exception as e:
            print(f"[ERROR][Amazon] Falló {q.isbn}: {e}")

        # 2) Fallback directo a DP/ISBN10 (print book)
        if isbn10:
            return _try_direct_dp(ax, session, isbn13, isbn10, max_retries=max_retries, base_wait=base_wait)

        return None
    finally:
        session.close()


def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    delay: float = 1.0,
    query_delay: float = 10.0,
    batch_size: int = 5,
    batch_pause: float = 120.0,
    max_retries: int = 2,
    base_wait: float = 12.0,
    output: str = DEFAULT_OUTPUT,
    **_kwargs: Any,
) -> List[dict]:
    """
    Ejecuta consultas por tandas desde archivo (1 ISBN por línea).
    Devuelve lista de dicts.
    Si detecta bloqueo fuerte, corta para no insistir.
    """
    ax = _exp()
    queries = ax.load_queries_from_file(query_file, limit=limit_queries)
    if not queries:
        print(f"[ERROR] No hay ISBNs válidos en {query_file}")
        return []

    results: List[dict] = []
    session = ax.make_session()
    try:
        for idx, q in enumerate(queries, start=1):
            _throttle(query_delay=query_delay, batch_size=batch_size, batch_pause=batch_pause)

            isbn13 = q.isbn if len(q.isbn) == 13 else ""
            isbn10 = q.isbn if len(q.isbn) == 10 else _isbn13_to_isbn10(isbn13)

            print("=" * 80)
            print(f"[Amazon][QUERY #{idx}] {q.isbn}")

            try:
                row = ax.search_amazon_books(
                    session,
                    q,
                    delay=delay,
                    max_retries=max_retries,
                    base_wait=base_wait,
                )
                if row:
                    results.append(row)
                    continue
            except ax.BlockedByAmazon as e:
                print(f"[BLOCKED][Amazon] {e}")
                # intentamos el fallback directo antes de cortar
            except Exception as e:
                print(f"[ERROR][Amazon] {q.isbn}: {e}")

            # Fallback directo
            row2 = _try_direct_dp(ax, session, isbn13, isbn10, max_retries=max_retries, base_wait=base_wait)
            if row2:
                results.append(row2)
                continue

            # si el bloqueo es fuerte y no hubo fallback, cortamos para no insistir
            # (si querés que NO corte, lo cambiamos a "continue")
            print("[INFO] Sin datos (bloqueo o no encontrado). Cortando para no agravar.")
            break

    finally:
        session.close()

    # Guardado simple a CSV (útil para test sin depender del exportador)
    if output:
        import csv
        outp = Path(output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        if results:
            fields = list(results[0].keys())
            with outp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in results:
                    w.writerow(r)
        else:
            outp.write_text("", encoding="utf-8")

    return results
