# app/main.py
from __future__ import annotations

import argparse
import csv
import importlib
import inspect
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from app.config import DB_PATH
from typing import Any, Dict, Iterable, List, Optional, Tuple


# -----------------------------------------------------------------------------
# Descubrimiento de sites
# -----------------------------------------------------------------------------

def discover_sites(package_name: str = "app.sites") -> Dict[str, Any]:
    """
    Descubre módulos dentro de app.sites que expongan SITE_ID y run_single/run_from_file.
    Evita cualquier cosa "experimental".
    """
    import pkgutil

    sites: Dict[str, Any] = {}
    pkg = importlib.import_module(package_name)

    for mod in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        name = mod.name
        if name.endswith(".experimental"):
            continue
        if ".experimental." in name:
            continue

        try:
            m = importlib.import_module(name)
        except Exception as e:
            print(f"[WARN] No se pudo importar {name}: {e}")
            continue

        site_id = getattr(m, "SITE_ID", None)
        if not site_id:
            continue
        if not hasattr(m, "run_single"):
            continue

        sites[site_id] = m

    return sites


# -----------------------------------------------------------------------------
# Coerción / normalización
# -----------------------------------------------------------------------------

def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    if is_dataclass(row):
        return asdict(row)
    if hasattr(row, "to_dict") and callable(getattr(row, "to_dict")):
        return row.to_dict()
    if hasattr(row, "__dict__"):
        return dict(row.__dict__)
    return {"value": row}


def coerce_rows(raw: Any) -> List[Dict[str, Any]]:
    """
    Convierte output del site en lista de dict.
    - None -> []
    - dict -> [dict]
    - lista -> [dict...]
    - Path/str CSV -> lee CSV y devuelve rows
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        out: List[Dict[str, Any]] = []
        for x in raw:
            out.append(_row_to_dict(x))
        return out

    if isinstance(raw, dict):
        return [raw]

    if isinstance(raw, (str, Path)):
        p = Path(raw)
        if p.exists() and p.suffix.lower() == ".csv":
            return read_csv_as_dicts(p)
        # string suelto
        return [{"value": str(raw)}]

    return [_row_to_dict(raw)]


# -----------------------------------------------------------------------------
# CSV helpers
# -----------------------------------------------------------------------------

def read_csv_as_dicts(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("[CSV] 0 filas, nada para escribir.")
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    # columnas = unión de keys
    cols: List[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                cols.append(k)
                seen.add(k)

    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# -----------------------------------------------------------------------------
# DB writer
# -----------------------------------------------------------------------------

def _map_site_row_to_db(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Genera el payload "raw" estándar que usa Database.upsert_rows().
    Soporta:
    - formato raw (el_lector style)
    - formato std (keys ISBN/TITULO/URL PORTADA…)
    - formato compuesto {"raw": {...}, "std": {...}, "qc": [...]}
    """
    r = dict(row)

    # Si viene en formato compuesto {"raw": {...}, "std": {...}, "qc": [...]},
    # mezclamos (sin pisar) para que el mapeo funcione igual.
    for key in ("raw", "std"):
        nested = r.get(key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                r.setdefault(k, v)

    # alias comunes
    url_detalle = (
        r.get("url_detalle")
        or r.get("url")
        or r.get("URL")
        or r.get("product_url")
        or r.get("url_bookpeople")
    )

    isbn = (
        r.get("isbn")
        or r.get("ISBN")
        or r.get("isbn_bookpeople")
        or r.get("isbn_yenny")
        or r.get("isbn_ellector")
    )

    titulo = (
        r.get("titulo")
        or r.get("TITULO")
        or r.get("titulo_bookpeople")
        or r.get("titulo_yenny")
        or r.get("titulo_ellector")
    )

    autor = (
        r.get("autor")
        or r.get("AUTOR")
        or r.get("autor_bookpeople")
        or r.get("autor_yenny")
        or r.get("autor_ellector")
    )

    editorial = (
        r.get("editorial")
        or r.get("EDITORIAL")
        or r.get("editorial_bookpeople")
        or r.get("editorial_yenny")
        or r.get("editorial_ellector")
    )

    descripcion = (
        r.get("descripcion")
        or r.get("SINOPSIS")
        or r.get("sinopsis")
        or r.get("descripcion_bookpeople")
        or r.get("descripcion_raw_html_bookpeople")
        or r.get("descripcion_yenny")
        or r.get("descripcion_ellector")
    )

    # precio / moneda
    precio = r.get("precio") or r.get("precio_bookpeople") or r.get("precio_yenny")
    moneda = r.get("moneda") or r.get("moneda_bookpeople") or r.get("moneda_yenny")

    # portada
    portada_url = (
        r.get("portada_url") or r.get("url_portada") or r.get("URL PORTADA") or r.get("URL_PORTADA")
        or r.get("url_portada_yenny") or r.get("cover_url") or r.get("image_url")
        or r.get("imagen_bookpeople") or r.get("URL_PORTADA")
    )

    sitio = r.get("sitio") or r.get("site") or r.get("SITE")

    info_adicional = r.get("info_adicional") or {}
    if not isinstance(info_adicional, dict):
        info_adicional = {}

    if moneda and "moneda" not in info_adicional:
        info_adicional["moneda"] = moneda
    if precio is not None and "precio" not in info_adicional:
        info_adicional["precio"] = precio

    # guardar también campos útiles que no entran directo
    # (por ejemplo editorial/formato/idioma/paginas/encuadernación)
    extras_map = {
        "editorial": ("editorial", "EDITORIAL"),
        "formato": ("formato", "FORMATO"),
        "idioma": ("idioma", "IDIOMA"),
        "encuadernacion": ("encuadernacion", "ENCUADERNACION", "ENCUADERNACIÓN", "encuadernacion_bookpeople"),
        "categoria": ("categoria", "CATEGORIA"),
        "paginas": ("paginas", "PAGINAS"),
        "fecha_publicacion": ("fecha_publicacion", "FECHA PUBLICACION", "FECHA PUBLICACIÓN"),
        "dimensiones": ("dimensiones", "DIMENSIONES"),
    }
    if isinstance(info_adicional, dict):
        for dest, sources in extras_map.items():
            if dest in info_adicional and info_adicional.get(dest) not in (None, ""):
                continue
            for src in sources:
                if src in r and r.get(src) not in (None, ""):
                    info_adicional[dest] = r.get(src)
                    break

    return {
        "url_detalle": url_detalle or "",
        "titulo": titulo or "",
        "autor": autor or "",
        "isbn": isbn or "",
        "precio": precio,
        "descripcion": descripcion or "",
        "info_adicional": info_adicional,
        "portada_url": portada_url or "",
        "portada_local": r.get("portada_local") or "",
        "sitio": sitio or "",
    }


def write_db(rows: List[Dict[str, Any]], db_path: str) -> Tuple[int, int]:
    from app.storage.book_std_db import Database

    db = Database(db_path)
    payload = []
    for r in rows:
        payload.append(_map_site_row_to_db(r))

    inserted, updated = db.upsert_rows(payload)
    return inserted, updated


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def cmd_sites(_args: argparse.Namespace) -> None:
    sites = discover_sites()
    for sid in sorted(sites.keys()):
        mod = sites[sid]
        print(f"- {sid}: {getattr(mod, 'SITE_NAME', mod.__name__)}")


def cmd_run(args: argparse.Namespace) -> None:
    sites = discover_sites()
    if args.site not in sites:
        print(f"[ERROR] Site desconocido: {args.site}")
        print("Usa: python -m app sites")
        sys.exit(2)

    mod = sites[args.site]

    raw: Any
    if args.query_file:
        raw = mod.run_from_file(
            args.query_file,
            limit_queries=args.limit,
            delay=args.delay,
            query_delay=args.query_delay,
            batch_size=args.batch_size,
            batch_pause=args.batch_pause,
            output=args.output,
        )
        # si el site devuelve Path CSV, lo convertimos a rows
        out_rows = coerce_rows(raw)
    else:
        raw = mod.run_single(args.query, delay=args.delay)
        out_rows = coerce_rows(raw)

    # CSV
    if args.output:
        out_path = Path(args.output)
        print(f"[INFO] Escribiendo CSV en {out_path}")
        write_csv(out_path, out_rows)
        print(f"[CSV] {len(out_rows)} filas escritas en {out_path}")

    print(f"[OK] Finalizado. Salida: {args.output}")

    # DB
    if args.write_db:
        inserted, updated = write_db(out_rows, args.db_path)
        print(f"[DB] upsert: {inserted} insertados, {updated} actualizados en {args.db_path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", description="BookSearchV2")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_sites = sub.add_parser("sites", help="Listar sites disponibles")
    p_sites.set_defaults(func=cmd_sites)

    p_run = sub.add_parser("run", help="Ejecutar búsqueda en un site")
    p_run.add_argument("--site", required=True)
    p_run.add_argument("--query", default="")
    p_run.add_argument("--query-file", default="")
    p_run.add_argument("--output", default="")
    p_run.add_argument("--delay", type=float, default=1.5)
    p_run.add_argument("--query-delay", type=float, default=0.0)
    p_run.add_argument("--batch-size", type=int, default=0)
    p_run.add_argument("--batch-pause", type=float, default=0.0)
    p_run.add_argument("--limit", type=int, default=None)

    p_run.add_argument("--write-db", action="store_true")
    p_run.add_argument("--db-path", default=str(DB_PATH))

    p_run.set_defaults(func=cmd_run)

    return p

def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)

if __name__ == "__main__":
    main()
