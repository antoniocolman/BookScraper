from __future__ import annotations

import argparse
import csv
import re
import time
import random
from dataclasses import dataclass, asdict
from pathlib import Path
from app.config import EXPORTS_DIR
from typing import Any, Dict, Iterable, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from app.utils.common import normalize_isbn
from . import yenny_core as core  # shared helpers


BASE_URL = "https://www.yenny-elateneo.com"

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
    image_url: Optional[str] = None
    portada_url: Optional[str] = None

    # extra campos que tu parser YA está pasando
    descripcion_corta: Optional[str] = None
    descripcion_raw: Optional[str] = None
    sinopsis: Optional[str] = None
    formato: Optional[str] = None
    editorial: Optional[str] = None
    encuadernacion: Optional[str] = None
    idioma: Optional[str] = None
    paginas: Optional[str] = None
    dimensiones: Optional[str] = None
    fecha_publicacion: Optional[str] = None
    sku: Optional[str] = None

    info_adicional: Optional[Dict[str, Any]] = None
    raw_data: Optional[Dict[str, Any]] = None

@dataclass
class MatchResult:
    query_raw: str
    isbn_archivo: Optional[str]
    titulo_archivo: Optional[str]

    isbn_yenny: Optional[str]
    titulo_yenny: Optional[str]
    autor_yenny: Optional[str]

    precio_yenny: Optional[float]
    moneda_yenny: Optional[str]

    url_yenny: Optional[str]
    imagen_yenny: Optional[str]
    url_portada_yenny: Optional[str]

    formato_yenny: Optional[str]
    editorial_yenny: Optional[str]
    encuadernacion_yenny: Optional[str]
    idioma_yenny: Optional[str]
    paginas_yenny: Optional[str]
    dimensiones_yenny: Optional[str]
    fecha_publicacion_yenny: Optional[str]
    descripcion_yenny: Optional[str]
    descripcion_raw_yenny: Optional[str]
    sinopsis_yenny: Optional[str]

    isbn_match: bool
    titulo_match_estricto: bool
    titulo_match_relajado: bool
    similitud_titulo: float

    motivo_seleccion: str


# -----------------------------------------------------------------------------
# Utilidades de normalización
# -----------------------------------------------------------------------------


def _normalize_isbn(isbn: Optional[str]) -> Optional[str]:
    # Compat: usa el normalizador común del proyecto
    if not isbn:
        return None
    v = normalize_isbn(isbn)
    return v or None


def _normalize_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = title.lower()
    t = t.replace("-", " ")
    t = re.sub(r"\s+", " ", t)
    t = t.strip()
    return t or None


# -----------------------------------------------------------------------------
# Lectura de archivo de consultas
# -----------------------------------------------------------------------------


def parse_query_line(line: str) -> Optional[QueryItem]:
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
        # línea simple: ISBN o título suelto
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
    """Delegado a yenny_core."""
    return core.make_session()


def fetch_html(session: requests.Session, url: str, timeout: float = 30.0, **kwargs) -> str:
    """Delegado a yenny_core (soporta JSON results_only)."""
    return core.fetch_html(session, url, timeout=timeout, **kwargs)


def fetch_html_with_backoff(
    session: requests.Session,
    url: str,
    max_retries: int = 3,
    base_wait: float = 8.0,
    timeout: float = 30.0,
) -> str:
    """Delegado a yenny_core."""
    return core.fetch_html_with_backoff(
        session,
        url,
        max_retries=max_retries,
        base_wait=base_wait,
        timeout=timeout,
    )


# -----------------------------------------------------------------------------
# Parsing HTML
# -----------------------------------------------------------------------------


def _normalize_url(href: str) -> str:
    return core.normalize_url(href, base_url=BASE_URL)


def extract_product_urls_from_listing(html: str) -> list[str]:
    return core.extract_product_urls_from_listing(html, base_url=BASE_URL)


def parse_product_page(html: str, url: str) -> BookRecord:
    """Parsea un producto usando el parser compartido y lo adapta al BookRecord de este módulo."""
    data = core.parse_product_page(html, url)

    info_adicional = {
        "formato": data.get("formato"),
        "editorial": data.get("editorial"),
        "encuadernacion": data.get("encuadernacion"),
        "idioma": data.get("idioma"),
        "paginas": data.get("paginas"),
        "fecha_publicacion": data.get("fecha_publicacion"),
        "sku": data.get("sku"),
        "raw_data": data.get("raw_data") or {},
    }

    paginas_val = data.get("paginas")
    paginas_str = str(paginas_val) if paginas_val is not None else None

    return BookRecord(
        url=url,
        title=data.get("title"),
        author=data.get("author"),
        isbn=data.get("isbn"),
        price=data.get("price"),
        currency=data.get("currency") or "ARS",
        image_url=data.get("image_url"),
        portada_url=data.get("portada_url"),
        descripcion_corta=None,
        descripcion_raw=data.get("descripcion_raw"),
        sinopsis=data.get("sinopsis"),
        formato=data.get("formato"),
        editorial=data.get("editorial"),
        encuadernacion=data.get("encuadernacion"),
        idioma=data.get("idioma"),
        paginas=paginas_str,
        fecha_publicacion=data.get("fecha_publicacion"),
        sku=data.get("sku"),
        info_adicional=info_adicional,
    )


# -----------------------------------------------------------------------------
# Lógica de búsqueda y ranking de resultados
# -----------------------------------------------------------------------------


def search_yenny(
    session: requests.Session,
    query: QueryItem,
    *,
    max_results: int = 5,
    delay: float = 1.5,
    max_retries: int = 2,
    base_wait: float = 5.0,
) -> Optional[MatchResult]:
    # Texto que mandamos a /search/?q=...
    if query.isbn:
        q_str = _normalize_isbn(query.isbn) or query.isbn
    elif query.title:
        q_str = query.title
    else:
        return None

    search_url = f"{BASE_URL}/search/?q={q_str}"
    print(f"[SEARCH] '{q_str}' -> {search_url}")

    try:
        html = fetch_html_with_backoff(
            session,
            search_url,
            max_retries=max_retries,
            base_wait=base_wait,
        )
    except Exception as e:
        print(f"[ERROR] No se pudo buscar '{q_str}': {e}")
        return None

    product_urls = extract_product_urls_from_listing(html)
    if not product_urls:
        print(f"[INFO] Sin resultados visibles para '{q_str}'.")
        return None

    product_urls = product_urls[:max_results]

    expected_isbn_norm = _normalize_isbn(query.isbn)
    expected_title_norm = _normalize_title(query.title)

    best_score = -1.0
    best_record: Optional[BookRecord] = None
    best_flags: Tuple[bool, bool, bool, float, str] = (False, False, False, 0.0, "")

    for idx, url in enumerate(product_urls, start=1):
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
        except Exception as e:
            print(f"    [ERROR] No se pudo parsear {url}: {e}")
            continue

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

        # Score para elegir el mejor candidato
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

        score += similitud  # 0..1
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

    if not best_record:
        print(f"[INFO] Ningún producto válido para '{q_str}'.")
        return None

    # Desempaquetamos los flags del mejor candidato
    (
        isbn_match_flag,
        titulo_estricto_flag,
        titulo_relajado_flag,
        similitud_val,
        motivo_val,
    ) = best_flags

    # Preparar campos extra (portada y sinopsis)
    portada_url = getattr(best_record, "portada_url", None) or best_record.image_url
    sinopsis = getattr(best_record, "sinopsis", None) or best_record.descripcion_corta

    # Construimos el resultado final
    result = MatchResult(
        query_raw=query.raw,
        isbn_archivo=query.isbn,
        titulo_archivo=query.title,

        isbn_yenny=best_record.isbn,
        titulo_yenny=best_record.title,
        autor_yenny=best_record.author,

        precio_yenny=best_record.price,
        moneda_yenny=best_record.currency,

        url_yenny=best_record.url,
        imagen_yenny=best_record.image_url,
        url_portada_yenny=portada_url,

        formato_yenny=best_record.formato,
        editorial_yenny=best_record.editorial,
        encuadernacion_yenny=best_record.encuadernacion,
        idioma_yenny=best_record.idioma,
        paginas_yenny=best_record.paginas,
        dimensiones_yenny=best_record.dimensiones,
        fecha_publicacion_yenny=best_record.fecha_publicacion,
        descripcion_yenny=best_record.descripcion_corta,
        descripcion_raw_yenny=best_record.descripcion_raw,
        sinopsis_yenny=sinopsis,

        isbn_match=isbn_match_flag,
        titulo_match_estricto=titulo_estricto_flag,
        titulo_match_relajado=titulo_relajado_flag,
        similitud_titulo=similitud_val,
        motivo_seleccion=motivo_val,
    )

    # Resumen corto en consola
    print("-" * 80)
    print(f"Título archivo : {query.title}")
    print(f"ISBN archivo   : {query.isbn}")
    print(f"Título Yenny   : {result.titulo_yenny}")
    print(f"ISBN Yenny     : {result.isbn_yenny}")
    print(f"Autor          : {result.autor_yenny}")
    print(f"Precio         : {result.precio_yenny} {result.moneda_yenny}")
    print(f"Formato        : {result.formato_yenny}")
    print(f"Editorial      : {result.editorial_yenny}")
    print(f"Encuadernación : {result.encuadernacion_yenny}")
    print(f"Idioma         : {result.idioma_yenny}")
    print(f"Páginas        : {result.paginas_yenny}")
    print(f"Dimensiones    : {result.dimensiones_yenny}")
    print(f"F. Publicación : {result.fecha_publicacion_yenny}")
    print(f"URL            : {result.url_yenny}")
    print(f"Imagen         : {result.imagen_yenny}")
    print(f"URL portada    : {result.url_portada_yenny}")
    print(f"Sinópsis       : {result.sinopsis_yenny}")
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
        "isbn_yenny",
        "titulo_yenny",
        "autor_yenny",
        "precio_yenny",
        "moneda_yenny",
        "url_yenny",
        "imagen_yenny",
        "url_portada_yenny",
        "formato_yenny",
        "editorial_yenny",
        "encuadernacion_yenny",
        "idioma_yenny",
        "paginas_yenny",
        "dimensiones_yenny",
        "fecha_publicacion_yenny",
        "descripcion_yenny",
        "descripcion_raw_yenny",
        "sinopsis_yenny",
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
        description="Búsqueda directa en Yenny / El Ateneo por ISBN / título (standalone)."
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Consulta única (ISBN o título).",
    )
    parser.add_argument(
        "--query-file",
        type=str,
        default=None,
        help=(
            "Ruta a archivo .txt con una consulta por línea. "
            "Formato recomendado: 'ISBN | TÍTULO'. "
            "También acepta solo ISBN o solo título."
        ),
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Máximo de productos a evaluar por consulta.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Pausa en segundos entre descargas de fichas de producto.",
    )
    parser.add_argument(
        "--query-delay",
        type=float,
        default=0.0,
        help="Pausa en segundos entre una consulta del archivo y la siguiente.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
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
        default=str(EXPORTS_DIR / "yenny_busqueda_resultados.csv"),
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

            res = search_yenny(
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