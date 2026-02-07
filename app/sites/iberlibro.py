# app/sites/iberlibro.py
from __future__ import annotations

"""Site wrapper: IberLibro.

Este wrapper usa el módulo experimental para:
- armar la búsqueda (kn=...)
- parsear SRP
- opcionalmente parsear detalle (/bd)
"""

from pathlib import Path
from app.config import EXPORTS_DIR
from typing import List, Optional, Dict, Any

SITE_ID = "iberlibro"
SITE_NAME = "IberLibro"
DEFAULT_OUTPUT = str(EXPORTS_DIR / "iberlibro_resultados.csv")


def _exp():
    # Import lazy para que el discovery no reviente si faltan deps opcionales.
    from .experimental import iberlibro_experimental as ix
    return ix


def run_single(
    query: str,
    *,
    delay: float = 1.5,
    max_retries: int = 3,
    base_wait: float = 6.0,
    max_results: int = 0,
    fetch_detail: bool = True,
) -> Optional[Dict[str, Any]]:
    ix = _exp()
    q = ix.parse_query_line(query)
    if not q:
        return None

    client = ix.make_client()
    try:
        return ix.search_iberlibro(
            client,
            q,
            delay=delay,
            max_retries=max_retries,
            base_wait=base_wait,
            max_results=max_results,
            fetch_detail=fetch_detail,
        )
    finally:
        client.close()


def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    delay: float = 1.5,
    max_retries: int = 3,
    base_wait: float = 6.0,
    max_results: int = 0,
    fetch_detail: bool = True,
) -> List[Dict[str, Any]]:
    ix = _exp()
    queries = ix.load_queries_from_file(Path(query_file), limit=limit_queries)
    if not queries:
        return []

    out: List[Dict[str, Any]] = []
    client = ix.make_client()
    try:
        for q in queries:
            try:
                row = ix.search_iberlibro(
                    client,
                    q,
                    delay=delay,
                    max_retries=max_retries,
                    base_wait=base_wait,
                    max_results=max_results,
                    fetch_detail=fetch_detail,
                )
                if row:
                    out.append(row)
            except Exception as e:
                # best-effort: no cortar todo el batch
                out.append(
                    {
                        "sitio": "iberlibro",
                        "query_raw": q.raw,
                        "url_detalle": q.url or "",
                        "titulo": "",
                        "autor": "",
                        "isbn": q.isbn or "",
                        "precio": None,
                        "moneda": "",
                        "portada_url": "",
                        "descripcion": "",
                        "info_adicional": {"error": str(e)},
                    }
                )
        return out
    finally:
        client.close()