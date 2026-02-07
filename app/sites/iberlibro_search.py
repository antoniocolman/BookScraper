from __future__ import annotations

"""Site wrapper: IberLibro (search batch).

Incluye throttle para evitar rate-limits y puede exportar a CSV (devuelve Path).
"""

from pathlib import Path
from app.config import EXPORTS_DIR
from typing import Any, Dict, List, Optional
import json
import threading
import time
import random

SITE_ID = "iberlibro_search"
SITE_NAME = "IberLibro (search)"
CAPABILITIES = ["query", "query-file"]
DEFAULT_OUTPUT = str(EXPORTS_DIR / "iberlibro_busqueda_resultados.csv")


def _exp():
    # OJO: usamos el mismo experimental (iberlibro_experimental.py)
    from .experimental import iberlibro_experimental as ix
    return ix


_LOCK = threading.Lock()
_LAST_CALL_AT = 0.0
_CALLS = 0


def _throttle(*, query_delay: float, batch_size: int, batch_pause: float) -> None:
    global _LAST_CALL_AT, _CALLS
    with _LOCK:
        now = time.time()

        if _LAST_CALL_AT and query_delay > 0:
            wait = query_delay - (now - _LAST_CALL_AT)
            if wait > 0:
                time.sleep(wait)

        _CALLS += 1
        if batch_size > 0 and batch_pause > 0 and _CALLS >= batch_size:
            time.sleep(batch_pause + random.uniform(0.2, batch_pause * 0.15))
            _CALLS = 0

        _LAST_CALL_AT = time.time()


def _write_csv(rows: List[Dict[str, Any]], out_path: Path) -> None:
    import csv

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return

    # mantener orden de aparición de columnas (como main.write_csv)
    fieldnames: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            rr = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    rr[k] = json.dumps(v, ensure_ascii=False)
                else:
                    rr[k] = v
            w.writerow(rr)


def run_single(
    query: str,
    *,
    delay: float = 1.2,
    query_delay: float = 2.0,
    batch_size: int = 4,
    batch_pause: float = 20.0,
    max_retries: int = 3,
    base_wait: float = 6.0,
    max_results: int = 0,
    fetch_detail: bool = True,
):
    ix = _exp()
    q = ix.parse_query_line(query)
    if not q:
        return None

    _throttle(query_delay=query_delay, batch_size=batch_size, batch_pause=batch_pause)

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
    output: str = DEFAULT_OUTPUT,
    delay: float = 1.2,
    query_delay: float = 2.0,
    batch_size: int = 4,
    batch_pause: float = 20.0,
    max_retries: int = 3,
    base_wait: float = 6.0,
    max_results: int = 0,
    fetch_detail: bool = True,
) -> Path:
    ix = _exp()
    p = Path(query_file)
    queries = ix.load_queries_from_file(p, limit=limit_queries)
    if not queries:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("", encoding="utf-8")
        return out

    rows: List[Dict[str, Any]] = []
    client = ix.make_client()
    try:
        for i, q in enumerate(queries, start=1):
            if i > 1:
                _throttle(query_delay=query_delay, batch_size=batch_size, batch_pause=batch_pause)

            try:
                r = ix.search_iberlibro(
                    client,
                    q,
                    delay=delay,
                    max_retries=max_retries,
                    base_wait=base_wait,
                    max_results=max_results,
                    fetch_detail=fetch_detail,
                )
                if r:
                    rows.append(r)
            except Exception as e:
                rows.append(
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
    finally:
        client.close()

    out = Path(output)
    _write_csv(rows, out)
    return out