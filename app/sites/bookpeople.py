from __future__ import annotations

"""
Wrapper estable para BookPeople (search).

Este módulo lo consume el CLI:
  - python -m app run --site bookpeople ...
  - python -m app site-help --site bookpeople

Cambios clave:
- Lazy import (_exp) para no romper discovery en "python -m app sites"
- Corrige type hints para Pylance (no usar bp.Algo en anotaciones)
- run_from_file ahora devuelve List[MatchResult] (no Path)
- CSV interno opcional (por defecto NO escribe)
"""

from pathlib import Path
from typing import Optional, List, TYPE_CHECKING

SITE_ID = "bookpeople"
SITE_NAME = "BookPeople"
DEFAULT_OUTPUT = "bookpeople_busqueda_resultados.csv"

if TYPE_CHECKING:
    from .experimental.bookpeople_search_experimental import QueryItem, MatchResult

def _exp():
    """
    Lazy import para que el CLI pueda listar sitios aunque el experimental
    tenga dependencias o esté en movimiento.

    Importante: NO hacer fallback a 'experimentales' (no existe) y
    NO ocultar errores de dependencias (ej. requests).
    """
    try:
        from .experimental import bookpeople_search_experimental as bp  # type: ignore
        return bp
    except ModuleNotFoundError as e:
        # Dependencia faltante (ej: requests)
        raise ModuleNotFoundError(
            f"{e}. Instalá dependencias: python -m pip install -r requirements.txt && python -m pip install requests"
        ) from e
    except ImportError as e:
        # Si el módulo experimental no existe / fue movido, mostrar error claro
        raise ImportError(
            f"No se pudo importar app.sites.experimental.bookpeople_search_experimental: {e}"
        ) from e

def run_single(
    query: str,
    *,
    max_results: int = 5,
    delay: float = 1.5,
    max_retries: int = 3,
    base_wait: float = 5.0,
) -> Optional["MatchResult"]:
    """
    Ejecuta una sola consulta contra BookPeople (ISBN, título o URL)
    y devuelve un MatchResult o None.
    """
    bp = _exp()
    q = bp.parse_query_line(query)
    if not q:
        print(f"[WARN] Consulta vacía o inválida: {query!r}")
        return None

    session = bp.make_session()
    try:
        return bp.search_bookpeople(
            session,
            q,
            max_results=max_results,
            delay=delay,
            max_retries=max_retries,
            base_wait=base_wait,
        )
    finally:
        session.close()


def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    max_results: int = 5,
    delay: float = 1.5,
    query_delay: float = 0.0,
    batch_size: int = 0,
    batch_pause: float = 0.0,
    max_retries: int = 3,
    base_wait: float = 5.0,
    # NUEVO: por defecto NO escribe CSV interno
    write_csv: bool = False,
    output: Optional[str] = None,
) -> List["MatchResult"]:
    """
    Procesa un archivo de consultas (ISBN | título | URL), devuelve lista de MatchResult.
    El CSV interno se escribe solo si write_csv=True o si output se proporciona.
    """
    bp = _exp()

    query_path = Path(query_file)
    queries: List["QueryItem"] = bp.load_queries_from_file(query_path, limit=limit_queries)
    if not queries:
        print(f"[ERROR] No se encontró ninguna consulta válida en {query_path}")
        return []

    session = bp.make_session()
    results: List["MatchResult"] = []
    try:
        import time

        for idx, q in enumerate(queries, start=1):
            # Pausa larga por tandas
            if batch_size > 0 and batch_pause > 0 and idx > 1 and (idx - 1) % batch_size == 0:
                print(
                    f"[INFO][BookPeople] Pausa de lote: ya se procesaron {idx-1} consultas, "
                    f"durmiendo {batch_pause:.1f}s..."
                )
                time.sleep(batch_pause)

            # Pausa corta entre consultas
            if idx > 1 and query_delay > 0:
                time.sleep(query_delay)

            print("=" * 80)
            print(f"[BookPeople][QUERY #{idx}] {q.raw}")

            res = bp.search_bookpeople(
                session,
                q,
                max_results=max_results,
                delay=delay,
                max_retries=max_retries,
                base_wait=base_wait,
            )
            if res:
                results.append(res)
    finally:
        session.close()

    # Solo escribir CSV interno si lo pedís explícitamente
    if write_csv or output:
        out_path = Path(output or DEFAULT_OUTPUT)
        bp.write_results_csv(out_path, results)

    return results
