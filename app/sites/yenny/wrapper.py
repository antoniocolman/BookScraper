from __future__ import annotations

from pathlib import Path
from app.config import EXPORTS_DIR
from typing import Any, List, Optional, TYPE_CHECKING
import time
import random
import threading

SITE_ID = "yenny_search"
SITE_NAME = "Yenny (search)"
CAPABILITIES = ["query", "query-file"]
DEFAULT_OUTPUT = str(EXPORTS_DIR / "yenny_busqueda_resultados.csv")

# Para que Pylance no se queje con "Variable not allowed in type expression"
if TYPE_CHECKING:
    from .engines.search import QueryItem, MatchResult  # noqa: F401


def _engine():
    # Lazy import: así "python -m app sites" no explota si engine tiene deps/errores
    from .engines import search as ye
    return ye


# -----------------------
# Throttle simple (sirve para sync + run_file)
# -----------------------
_LOCK = threading.Lock()
_CALLS_SINCE_PAUSE = 0
_LAST_CALL_AT = 0.0


def _throttle(*, query_delay: float, batch_size: int, batch_pause: float) -> None:
    """
    - query_delay: mínimo entre consultas (no entre productos).
    - batch_size/batch_pause: cada N consultas, pausa fuerte.
    """
    global _CALLS_SINCE_PAUSE, _LAST_CALL_AT

    with _LOCK:
        now = time.time()

        # respetar delay mínimo entre queries
        if _LAST_CALL_AT and query_delay > 0:
            wait = query_delay - (now - _LAST_CALL_AT)
            if wait > 0:
                time.sleep(wait)

        _CALLS_SINCE_PAUSE += 1

        # cada N consultas, dormir fuerte
        if batch_size > 0 and batch_pause > 0 and _CALLS_SINCE_PAUSE >= batch_size:
            jitter = random.uniform(0, batch_pause * 0.15)
            sleep_s = batch_pause + jitter
            print(f"[INFO][Yenny] Throttle: {batch_size} consultas -> durmiendo {sleep_s:.1f}s...")
            time.sleep(sleep_s)
            _CALLS_SINCE_PAUSE = 0

        _LAST_CALL_AT = time.time()


def run_single(
    query: str,
    *,
    max_results: int = 5,
    delay: float = 1.5,
    # defaults más conservadores para Yenny:
    query_delay: float = 2.5,
    batch_size: int = 4,
    batch_pause: float = 25.0,
    max_retries: int = 3,
    base_wait: float = 8.0,
):
    """
    Ejecuta una consulta (ideal para sync).

    FIXES:
    - Evita el warning de Pylance (tipos)
    - Agrega throttle por defecto (cada 4 consultas duerme ~25s)
    - No deja que un 429 te rompa el proceso: devuelve None en error
    """
    ye = _engine()
    q = ye.parse_query_line(query)
    if not q:
        print(f"[WARN] Consulta inválida: {query!r}")
        return None

    _throttle(query_delay=query_delay, batch_size=batch_size, batch_pause=batch_pause)

    session = ye.make_session()
    try:
        try:
            return ye.search_yenny(
                session,
                q,
                max_results=max_results,
                delay=delay,
                max_retries=max_retries,
                base_wait=base_wait,
            )
        except Exception as e:
            print(f"[ERROR][Yenny] Falló consulta {q.raw!r}: {e}")
            return None
    finally:
        session.close()


def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    max_results: int = 5,
    delay: float = 1.5,
    # defaults más conservadores para evitar 429
    query_delay: float = 2.5,
    batch_size: int = 4,
    batch_pause: float = 25.0,
    max_retries: int = 3,
    base_wait: float = 8.0,
    output: str = DEFAULT_OUTPUT,
) -> Path:
    """
    Lee consultas de archivo y genera CSV.

    FIX:
    - Maneja errores por consulta sin cortar todo el batch.
    - Defaults anti-429 (4 consultas -> pausa ~25s)
    """
    ye = _engine()
    query_path = Path(query_file)
    queries = ye.load_queries_from_file(query_path, limit=limit_queries)
    if not queries:
        print(f"[ERROR] No hay consultas válidas en {query_path}")
        return Path(output)

    session = ye.make_session()
    results: List[Any] = []
    try:
        for idx, q in enumerate(queries, start=1):
            if idx > 1:
                _throttle(query_delay=query_delay, batch_size=batch_size, batch_pause=batch_pause)

            print("=" * 80)
            print(f"[Yenny][QUERY #{idx}] {q.raw}")

            try:
                res = ye.search_yenny(
                    session,
                    q,
                    max_results=max_results,
                    delay=delay,
                    max_retries=max_retries,
                    base_wait=base_wait,
                )
                if res:
                    results.append(res)
            except Exception as e:
                print(f"[ERROR][Yenny] Consulta fallida {q.raw!r}: {e}")
                # seguimos con la siguiente
                continue
    finally:
        session.close()

    out_path = Path(output)
    ye.write_results_csv(out_path, results)
    return out_path


__all__ = [
    "SITE_ID",
    "SITE_NAME",
    "CAPABILITIES",
    "DEFAULT_OUTPUT",
    "run_single",
    "run_from_file",
]
