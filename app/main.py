#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI maestro (python -m app) para ejecutar scrapers "site" de app/sites/
y opcionalmente sincronizar contra DB (SQLite) y exportar.

Comandos:
  - sites        : lista sitios disponibles
  - site-help    : muestra ayuda de un sitio
  - run          : ejecuta un sitio (query o query-file) y exporta CSV
  - sync         : DB-first: busca ISBNs en DB, scrappea faltantes, upsertea, exporta
  - export       : exporta desde DB por ISBNs (query / query-file) o todo
  - clean        : post-procesa CSV (ej. limpiar HTML / aplanar JSON)
  - db           : herramientas DB (ej: qc-report)
"""

from __future__ import annotations

import argparse
import csv
import json
import inspect
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, is_dataclass, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import importlib
import pkgutil

# -----------------------
# Helpers generales
# -----------------------

def _repo_root() -> Path:
    # app/main.py -> repo root es .. (parent of app)
    return Path(__file__).resolve().parents[1]

def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)

def normalize_isbn(value: Any) -> str:
    """Normaliza ISBN para comparar: deja solo [0-9X] en mayúscula."""
    if value is None:
        return ""
    s = str(value).strip().upper()
    s = re.sub(r"[^0-9X]", "", s)
    return s

def _is_isbnish(s: Any) -> bool:
    s = normalize_isbn(s)
    return len(s) in (10, 13) and all(ch.isdigit() or ch == "X" for ch in s)

def _str2bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "si", "sí"):
        return True
    if s in ("0", "false", "f", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"Valor booleano inválido: {v} (usa True/False)")

def strip_html(text: Any) -> str:
    """Remueve tags HTML de forma simple (sin depender de bs4)."""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"<\s*/\s*p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    try:
        import html as _html
        s = _html.unescape(s)
    except Exception:
        pass
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s

def resolve_query_file(query_file: str) -> Path:
    """
    Permite pasar solo el nombre del archivo. Si no existe en cwd,
    lo busca en carpetas estándar:
      - data/queries/
      - data/queries/isbn/
      - data/queries/titulos/
      - data/queries/mix/
      - queries/
    """
    p = Path(query_file)
    if p.exists():
        return p

    root = _repo_root()
    candidates = [
        root / "data" / "queries" / query_file,
        root / "data" / "queries" / "isbn" / query_file,
        root / "data" / "queries" / "titulos" / query_file,
        root / "data" / "queries" / "mix" / query_file,
        root / "queries" / query_file,
    ]
    for c in candidates:
        if c.exists():
            return c

    raise FileNotFoundError(f"Archivo de consultas no existe: {query_file}")

def load_isbns_from_query_file(path: Path, limit: Optional[int] = None) -> List[str]:
    """
    Lee ISBNs desde un TXT:
      - Soporta líneas 'ISBN' o 'ISBN<TAB>TITULO' o 'ISBN | TITULO'
      - Ignora vacías y comentarios (#)
    """
    isbns: List[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            token = re.split(r"[\t;|,]", raw, maxsplit=1)[0].strip()
            token2 = raw.split()[0].strip() if raw.split() else token
            candidate = token if _is_isbnish(token) else token2
            if _is_isbnish(candidate):
                isbns.append(normalize_isbn(candidate))
            if limit and len(isbns) >= limit:
                break

    # quitar duplicados manteniendo orden
    seen: Set[str] = set()
    out: List[str] = []
    for x in isbns:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def write_csv(rows: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with out_path.open("w", newline="", encoding="utf-8") as f:
            f.write("")
        return

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

# -----------------------
# Sites
# -----------------------

@dataclass
class SiteEntry:
    site_id: str
    site_name: str
    module_path: str
    capabilities: Tuple[str, ...]
    run_single: Optional[Any] = None
    run_from_file: Optional[Any] = None

    def help(self) -> str:
        doc = ''
        try:
            mod = importlib.import_module(self.module_path)
            doc = (getattr(mod, 'HELP', '') or getattr(mod, '__doc__', '') or '').strip()
        except Exception:
            pass

        parts: List[str] = []
        parts.append(f"== {self.site_id} :: {self.site_name} ==")
        parts.append('')
        if doc:
            parts.append(doc)
            parts.append('')

        try:
            if callable(self.run_single):
                parts.append('run_single' + str(inspect.signature(self.run_single)))
            if callable(self.run_from_file):
                parts.append('run_from_file' + str(inspect.signature(self.run_from_file)))
        except Exception:
            pass

        caps = ', '.join(self.capabilities) if self.capabilities else '-'
        parts.append('')
        parts.append('capabilities: ' + caps)
        parts.append('module: ' + self.module_path)
        return '\n'.join(parts)

def _discover_sites_inline(verbose: bool = False) -> List[SiteEntry]:
    entries: List[SiteEntry] = []

    pkg = importlib.import_module('app.sites')
    for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + '.'):
        modname = m.name

        if modname.endswith('.__init__'):
            continue
        if modname.endswith('.manager_sites'):
            continue
        if modname.endswith('.experimental') or '.experimental.' in modname:
            continue

        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            if verbose:
                _stderr(f"[DISCOVER] No se pudo importar {modname}: {e}")
            continue

        site_id = getattr(mod, 'SITE_ID', None)
        if not site_id:
            continue

        site_name = getattr(mod, 'SITE_NAME', None) or str(site_id)
        run_single = getattr(mod, 'run_single', None)
        run_from_file = getattr(mod, 'run_from_file', None)

        caps: List[str] = []
        if callable(run_single):
            caps.append('query')
        if callable(run_from_file):
            caps.append('query-file')
        if not caps:
            continue

        entries.append(
            SiteEntry(
                site_id=str(site_id),
                site_name=str(site_name),
                module_path=modname,
                capabilities=tuple(caps),
                run_single=run_single if callable(run_single) else None,
                run_from_file=run_from_file if callable(run_from_file) else None,
            )
        )

    entries.sort(key=lambda x: x.site_id)
    return entries

def _discover_sites(verbose: bool = False):
    try:
        from app.sites.manager_sites import discover_sites
        return discover_sites()
    except Exception as e:
        if verbose:
            _stderr(f"[DISCOVER] manager_sites no disponible ({e}); usando discovery inline")
        return _discover_sites_inline(verbose=verbose)

def _get_site(site_id: str, verbose: bool = False):
    sites = {s.site_id: s for s in _discover_sites(verbose=verbose)}
    if site_id not in sites:
        raise SystemExit(f"[ERROR] Site '{site_id}' no existe. Usá: python -m app sites")
    return sites[site_id]

def _safe_call(fn, *args, **kwargs):
    if not callable(fn):
        return None
    sig = inspect.signature(fn)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params and v is not None}
    return fn(*args, **filtered)

def _safe_run_single(site_entry, query, **kwargs):
      
    fn = site_entry.run_single
    if not callable(fn):
        raise RuntimeError(f"Site '{site_entry.site_id}' no tiene run_single")

    sig = inspect.signature(fn)
    params = sig.parameters

    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(query, **kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params and v is not None}
    return fn(query, **filtered)

def _safe_run_from_file(site_entry, query_file, **kwargs):
    fn = site_entry.run_from_file
    if not callable(fn):
        raise RuntimeError(f"Site '{site_entry.site_id}' no tiene run_from_file")

    sig = inspect.signature(fn)
    params = sig.parameters

    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(query_file, **kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params and v is not None}
    return fn(query_file, **filtered)

def _read_csv_as_dicts(p: Path) -> List[Dict[str, Any]]:
    if not p.exists() or not p.is_file():
        return []
    with p.open('r', encoding='utf-8', errors='ignore', newline='') as f:
        return list(csv.DictReader(f))

def _row_to_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if is_dataclass(x):
        return asdict(x)
    try:
        return dict(x)
    except Exception:
        return {"value": x}

def coerce_rows(obj: Any) -> List[Dict[str, Any]]:
    if obj is None:
        return []

    if isinstance(obj, (str, Path)):
        try:
            p = Path(obj)
            if p.exists() and p.is_file() and p.suffix.lower() == '.csv':
                return _read_csv_as_dicts(p)
        except Exception:
            pass

    if isinstance(obj, dict) or is_dataclass(obj):
        return [_row_to_dict(obj)]

    if isinstance(obj, (list, tuple)):
        out: List[Dict[str, Any]] = []
        for x in obj:
            if x is None:
                continue
            out.append(_row_to_dict(x))
        return out

    if hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes, dict)):
        try:
            out: List[Dict[str, Any]] = []
            for x in obj:
                if x is None:
                    continue
                out.append(_row_to_dict(x))
            return out
        except TypeError:
            pass

    return [_row_to_dict(obj)]

# -----------------------
# DB
# -----------------------

def _get_db(db_path: str):
    from app.storage.book_std_db import Database
    return Database(db_path)

def _unwrap_wrapper_row(row: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Si viene como wrapper:
      {"raw": {...}, "std": {...}, "qc":[...]}
    devolvemos (raw_dict, full_wrapper).
    Si no, devolvemos (row, None).
    """
    if isinstance(row, dict) and isinstance(row.get("raw"), dict):
        return dict(row["raw"]), row
    return dict(row), None

def _map_site_row_to_db(row: Dict[str, Any], sitio: str) -> Dict[str, Any]:
    """
    Normaliza claves de scrapers a esquema "legacy" que consume book_std_db.py.
    Devuelve dict con:
      url_detalle, titulo, autor, isbn, precio, descripcion, info_adicional,
      portada_url, portada_local, sitio, raw_json (opcional)
    """
    base, wrapper = _unwrap_wrapper_row(row)
    r = dict(base)

    # Si el wrapper tiene std/qc, lo guardamos (sirve para qc-report)
    raw_json = ""
    if wrapper is not None:
        try:
            raw_json = json.dumps(wrapper, ensure_ascii=False)
        except Exception:
            raw_json = ""

    def pick(*keys: str):
        for k in keys:
            v = r.get(k)
            if v not in (None, "", [], {}):
                return v
        # fallback: si el wrapper trae algo arriba
        if isinstance(row, dict):
            for k in keys:
                v = row.get(k)
                if v not in (None, "", [], {}):
                    return v
        return ""

    # --- URL / Título / Autor / ISBN ---
    url_detalle = pick(
        "url_detalle", "url", "URL",
        "url_yenny", "url_bookpeople", "url_lector"
    )

    titulo = pick(
        "titulo", "Título", "TITULO",
        "titulo_yenny", "titulo_bookpeople",
        "titulo_encontrado", "name", "title"
    )

    autor = pick(
        "autor", "Autor", "AUTOR",
        "autor_yenny", "autor_bookpeople",
        "author"
    )

    isbn = pick(
        "isbn", "ISBN",
        "isbn_yenny", "isbn_bookpeople",
        "isbn_archivo", "isbn_encontrado",
        "ean", "gtin13"
    )

    precio = pick(
        "precio", "Precio",
        "precio_ars",
        "precio_yenny", "precio_bookpeople",
        "price"
    )

    # --- Descripción / Sinopsis ---
    descripcion = pick(
        "descripcion", "SINOPSIS", "sinopsis",
        "sinopsis_yenny", "descripcion_yenny", "descripcion_raw_yenny",
        "descripcion_bookpeople"
    ) or ""

    # --- Portada ---
    portada_url = pick(
        "portada_url", "cover_url", "image_url", "image",
        "url_portada_yenny", "imagen_yenny",
        "imagen_bookpeople"
    )
    portada_local = pick("portada_local") or ""

    # --- info_adicional ---
    info_adicional = r.get("info_adicional") or {}
    if isinstance(info_adicional, str):
        try:
            info_adicional = json.loads(info_adicional)
        except Exception:
            info_adicional = {"raw": info_adicional}
    if not isinstance(info_adicional, dict):
        info_adicional = {}

    def put_if(dest_key: str, value: Any):
        if value not in (None, "", [], {}):
            info_adicional.setdefault(dest_key, value)

    # Homogeneizamos alias típicos (por sitio o por naming)
    alias_map = {
        "editorial": ["editorial", "EDITORIAL", "editorial_yenny", "editorial_bookpeople"],
        "formato": ["formato", "formato_yenny", "formato_bookpeople"],
        "encuadernacion": [
            "encuadernacion", "encuadernacion_yenny", "encuadernacion_bookpeople",
            "binding", "binding_bookpeople",
        ],
        "idioma": ["idioma", "IDIOMA", "idioma_yenny", "idioma_bookpeople"],
        "paginas": ["paginas", "PAGINAS", "paginas_yenny", "paginas_bookpeople"],
        "dimensiones": ["dimensiones", "DIMENSIONES", "dimensiones_yenny", "dimensiones_bookpeople", "medidas"],
        "fecha_publicacion": [
            "fecha_publicacion", "FECHA PUBLICACION",
            "fecha_publicacion_yenny", "fecha_publicacion_bookpeople", "fecha_publicacion_iso_bookpeople"
        ],
        "categoria": ["categoria", "categoría", "category"],
    }

    for dest, sources in alias_map.items():
        if info_adicional.get(dest) not in (None, "", [], {}):
            continue
        for sk in sources:
            val = pick(sk)
            if val not in (None, "", [], {}):
                put_if(dest, val)
                break

    # conservar precio por si querés analizar luego
    put_if("precio", precio)

    out = {
        "url_detalle": str(url_detalle or "").strip(),
        "titulo": str(titulo or "").strip(),
        "autor": str(autor or "").strip(),
        "isbn": normalize_isbn(isbn or ""),
        "precio": precio,
        "descripcion": str(descripcion or ""),
        "info_adicional": info_adicional,
        "portada_url": str(portada_url or "").strip(),
        "portada_local": str(portada_local or "").strip(),
        "sitio": sitio,
    }

    if raw_json:
        out["raw_json"] = raw_json

    return out

# -----------------------
# QC helpers (raw_json)
# -----------------------

def iter_qc_issues_from_raw_json(raw_json: str) -> List[Dict[str, Any]]:
    """
    Extrae lista de issues QC desde raw_json guardado en book_std.raw_json.
    Soporta:
      - {"raw":..., "std":..., "qc":[{code,severity,message},...]}
      - legacy sin qc -> []
    """
    if not raw_json:
        return []
    try:
        obj = json.loads(raw_json)
    except Exception:
        return []

    if isinstance(obj, dict):
        qc = obj.get("qc")
        if isinstance(qc, list):
            return [it for it in qc if isinstance(it, dict)]
        return []
    if isinstance(obj, list):
        return [it for it in obj if isinstance(it, dict)]
    return []

def cmd_db(args: argparse.Namespace) -> int:
    if not getattr(args, "qc_report", False):
        raise SystemExit("[ERROR] db requiere una acción. Usá --qc-report")

    db_path = args.db_path
    out_path = Path(args.output) if args.output else (_repo_root() / "data" / "exports" / "qc_report.csv")
    details_path = Path(args.details) if args.details else None
    details_limit = int(args.details_limit or 0)
    site_filter = (args.site or "").strip().lower()
    only_warn = bool(getattr(args, "only_warn", False))
    min_count = int(getattr(args, "min_count", 1) or 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if details_path:
        details_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    sql = "SELECT site, isbn, titulo, url, raw_json FROM book_std WHERE raw_json IS NOT NULL AND raw_json <> ''"
    params: List[Any] = []
    if site_filter:
        sql += " AND lower(site)=?"
        params.append(site_filter)

    cur = con.execute(sql, params)

    counts: Counter = Counter()
    totals: Counter = Counter()
    detail_rows: List[Dict[str, Any]] = []
    detail_written = 0

    for row in cur:
        site = (row["site"] or "").strip()
        isbn = row["isbn"] or ""
        titulo = row["titulo"] or ""
        url = row["url"] or ""
        raw_json_s = row["raw_json"] or ""

        issues = iter_qc_issues_from_raw_json(raw_json_s)
        if not issues:
            continue

        for it in issues:
            code = str(it.get("code") or "").strip() or "UNKNOWN"
            sev = str(it.get("severity") or "").strip() or "info"
            msg = str(it.get("message") or "").strip()

            if only_warn and sev != "warn":
                continue

            counts[(site, code, sev)] += 1
            totals[(site, "__TOTAL__", "__TOTAL__")] += 1

            if details_path and (details_limit <= 0 or detail_written < details_limit):
                detail_rows.append({
                    "SITE": site,
                    "ISBN": isbn,
                    "TITULO": titulo,
                    "URL": url,
                    "CODE": code,
                    "SEVERITY": sev,
                    "MESSAGE": msg,
                })
                detail_written += 1

    con.close()

    report_rows: List[Dict[str, Any]] = []
    for (site, code, sev), c in totals.items():
        report_rows.append({"SITE": site, "CODE": code, "SEVERITY": sev, "COUNT": c})

    for (site, code, sev), c in counts.items():
        if c < min_count:
            continue
        report_rows.append({"SITE": site, "CODE": code, "SEVERITY": sev, "COUNT": c})

    def _sort_key(r):
        is_total = (r["CODE"] == "__TOTAL__")
        return (r["SITE"], 0 if is_total else 1, -int(r["COUNT"]), r["CODE"], r["SEVERITY"])

    report_rows.sort(key=_sort_key)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["SITE", "CODE", "SEVERITY", "COUNT"])
        w.writeheader()
        for r in report_rows:
            w.writerow(r)

    print(f"[QC] Reporte agregado: {len(report_rows)} filas -> {out_path}")

    if details_path:
        with details_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["SITE", "ISBN", "TITULO", "URL", "CODE", "SEVERITY", "MESSAGE"])
            w.writeheader()
            for r in detail_rows:
                w.writerow(r)
        print(f"[QC] Detalle: {len(detail_rows)} filas -> {details_path}")
        if details_limit > 0 and detail_written >= details_limit:
            print(f"[QC] Nota: detalle limitado a {details_limit} filas (--details-limit).")

    return 0

# -----------------------
# Commands
# -----------------------

def cmd_sites(_args: argparse.Namespace) -> int:
    sites = _discover_sites()
    print("Sitios disponibles:\n")
    for s in sites:
        caps = ", ".join(s.capabilities) if s.capabilities else "-"
        print(f"  - {s.site_id:<14} | {s.site_name:<22} | {caps:<15} | {s.module_path}")
    return 0

def cmd_site_help(args: argparse.Namespace) -> int:
    site = _get_site(args.site, verbose=getattr(args, 'verbose', False))
    if hasattr(site, 'help') and callable(getattr(site, 'help')):
        print(site.help())
    else:
        caps = ', '.join(getattr(site, 'capabilities', []) or []) or '-'
        mod = getattr(site, 'module_path', '') or getattr(site, 'module', '')
        print(f"Site: {site.site_id}\nModule: {mod}\nCapabilities: {caps}")
    return 0

def cmd_run(args: argparse.Namespace) -> int:
    site = _get_site(args.site)

    kwargs = dict(
        max_results=args.max_results,
        delay=args.delay,
        query_delay=args.query_delay,
        batch_size=args.batch_size,
        batch_pause=args.batch_pause,
        limit_queries=(args.limit_queries if args.limit_queries and args.limit_queries > 0 else None),
        fetch_detail=getattr(args, "fetch_detail", None),  # <-- ESTA
    )

    if args.query:
        raw = _safe_call(site.run_single, args.query, **kwargs)
    elif args.query_file:
        qf = resolve_query_file(args.query_file)
        raw = _safe_call(site.run_from_file, str(qf), **kwargs)
    else:
        raise SystemExit("[ERROR] Debés usar --query o --query-file")

    out_rows = coerce_rows(raw)

    if args.output:
        out_path = Path(args.output)
        print(f"[INFO] Escribiendo CSV en {out_path}")
        write_csv(out_rows, out_path)
        print(f"[CSV] {len(out_rows)} filas escritas en {out_path}")
        print(f"[OK] Finalizado. Salida: {out_path}")

    if args.write_db:
        db = _get_db(args.db_path)
        payload = [_map_site_row_to_db(r, sitio=args.site) for r in out_rows]
        if hasattr(db, "upsert_many"):
            inserted, updated = db.upsert_many(payload)
        elif hasattr(db, "upsert_batch"):
            inserted, updated = db.upsert_batch(payload)
        else:
            raise AttributeError("Database no tiene upsert_many ni upsert_batch")
        db.close()
        print(f"[DB] upsert: {inserted} insertados, {updated} actualizados en {args.db_path}")

    if not args.output:
        print(json.dumps(out_rows[:5], ensure_ascii=False, indent=2))
        if len(out_rows) > 5:
            print(f"... ({len(out_rows)} filas en total)")

    return 0

def cmd_sync(args: argparse.Namespace) -> int:
    
    site_params = {}
    sp = (getattr(args, "site_params_file", "") or "").strip()
    if sp and Path(sp).exists():
        site_params = json.loads(Path(sp).read_text(encoding="utf-8"))
    default_p = site_params.get("_default", {})
    
    if not (args.query or args.query_file):
        raise SystemExit("[ERROR] Debés usar --query o --query-file")

    if args.query:
        raw_isbns = [normalize_isbn(args.query)]
    else:
        qf = resolve_query_file(args.query_file)
        raw_isbns = load_isbns_from_query_file(qf, limit=args.limit_queries)

    isbns = [x for x in raw_isbns if _is_isbnish(x)]
    if not isbns:
        raise SystemExit("[ERROR] No se encontró ningún ISBN válido para sync.")

    db = _get_db(args.db_path)

    present = db.isbns_present(isbns)
    missing = [x for x in isbns if x not in present]
    print(f"[SYNC] Total: {len(isbns)} | en DB: {len(present)} | faltan: {len(missing)}")

    if not missing and getattr(args, "remaining_out", ""):
        outp = Path(args.remaining_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text("", encoding="utf-8")
        print(f"[SYNC] remaining -> {outp} (0)")

    sites: List[str] = []
    for s in (args.sites or []):
        for part in str(s).split(","):
            part = part.strip()
            if part:
                sites.append(part)
 
    seen: Set[str] = set()
    sites = [s for s in sites if not (s in seen or seen.add(s))]

    if missing and not sites:
        raise SystemExit("[ERROR] Hay ISBNs faltantes pero no pasaste --sites para scrappear.")
    scraped_rows: List[Dict[str, Any]] = []
    remaining: List[str] = []

    if missing:
        remaining = list(missing)

        for site_id in sites:
            if not remaining:
                break

            site = _get_site(site_id)
            caps = set(site.capabilities or ())

            # Params efectivos por sitio (desde site_params.json)
            eff: Dict[str, Any] = {}
            eff.update(default_p or {})
            eff.update((site_params or {}).get(site_id, {}))

            eff_max_results = int(eff.get("max_results", getattr(args, "max_results", 4)) or 4)
            eff_delay = float(eff.get("delay", getattr(args, "delay", 2.0)) or 0.0)
            eff_query_delay = float(eff.get("query_delay", getattr(args, "query_delay", 1.0)) or 0.0)
            eff_batch_size = int(eff.get("batch_size", getattr(args, "batch_size", 0)) or 0)
            eff_batch_pause = float(eff.get("batch_pause", getattr(args, "batch_pause", 0.0)) or 0.0)

            # Si no soporta ni query ni query-file, saltamos
            if ("query" not in caps) and ("query-file" not in caps):
                print(f"[SYNC] {site_id}: no soporta 'query' ni 'query-file' (saltando)")
                continue

            # --- BATCH REAL: si batch_size > 0 y el sitio soporta run_from_file ---
            use_batch = (eff_batch_size > 0) and ("query-file" in caps) and callable(getattr(site, "run_from_file", None))

            if use_batch:
                import tempfile

                tmp_dir = Path(tempfile.mkdtemp(prefix=f"bsync_{site_id}_"))
                qpath = tmp_dir / "queries.txt"
                outcsv = tmp_dir / "out.csv"

                qpath.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                print(f"[SYNC] {site_id}: batch run_from_file n={len(remaining)} batch={eff_batch_size} pause={eff_batch_pause}")

                try:
                    ret = _safe_run_from_file(
                        site,
                        str(qpath),
                        max_results=eff_max_results,
                        delay=eff_delay,
                        query_delay=eff_query_delay,
                        batch_size=eff_batch_size,
                        batch_pause=eff_batch_pause,
                        limit_queries=len(remaining),
                        output=str(outcsv),  # si el sitio soporta output, lo usará; si no, se ignora
                    )

                    # Obtener filas: preferimos lo retornado; si no retorna nada pero escribió out.csv, lo leemos
                    norm_rows = coerce_rows(ret)
                    if not norm_rows and outcsv.exists():
                        norm_rows = _read_csv_as_dicts(outcsv)

                    # Registrar filas + detectar ISBN encontrados
                    found: Set[str] = set()
                    for d in coerce_rows(norm_rows):
                        d.setdefault("sitio", site_id)
                        d.setdefault("_src_site", site_id)
                        scraped_rows.append(d)

                        cand = (
                            d.get("ISBN") or d.get("isbn") or d.get("Isbn")
                            or d.get("ean") or d.get("gtin13")
                        )
                        if cand:
                            n = normalize_isbn(str(cand))
                            if n:
                                found.add(n)

                    # Reducir remaining según lo encontrado
                    if found:
                        remaining = [x for x in remaining if normalize_isbn(x) not in found]

                except Exception as e:
                    _stderr(f"[SYNC] {site_id}: error batch: {e}")

            else:
                # --- MODO NORMAL: run_single por ISBN ---
                if "query" not in caps:
                    print(f"[SYNC] {site_id}: no soporta 'query' (saltando)")
                    continue

                still_missing: List[str] = []
                for isbn in remaining:
                    try:
                        rows = _safe_run_single(
                            site,
                            isbn,
                            max_results=eff_max_results,
                            delay=eff_delay,
                            query_delay=eff_query_delay,
                            batch_size=eff_batch_size,
                            batch_pause=eff_batch_pause,
                            fetch_detail=getattr(args, "fetch_detail", None),
                            limit_queries=1,
                        )
                        norm_rows = coerce_rows(rows)
                        if norm_rows:
                            d = norm_rows[0]  # mantenemos 1ra fila
                            d.setdefault("sitio", site_id)
                            d.setdefault("_src_site", site_id)
                            scraped_rows.append(d)
                        else:
                            still_missing.append(isbn)
                    except Exception as e:
                        _stderr(f"[SYNC] {site_id}: error en {isbn}: {e}")
                        still_missing.append(isbn)

                remaining = still_missing

            if remaining:
                print(f"[SYNC] Luego de {site_id}: siguen faltando {len(remaining)} ISBN(s).")

        if remaining:
            print(f"[SYNC] No se pudieron obtener {len(remaining)} ISBN(s): {', '.join(remaining[:10])}{'...' if len(remaining)>10 else ''}")

        if getattr(args, "remaining_out", ""):
            outp = Path(args.remaining_out)
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
            print(f"[SYNC] remaining -> {outp} ({len(remaining)})")

        payload = [_map_site_row_to_db(r, sitio=(r.get("sitio") or r.get("_src_site") or "")) for r in scraped_rows]
        if hasattr(db, "upsert_many"):
            inserted, updated = db.upsert_many(payload)
        elif hasattr(db, "upsert_batch"):
            inserted, updated = db.upsert_batch(payload)
        else:
            raise AttributeError("Database no tiene upsert_many ni upsert_batch")
        print(f"[DB] upsert: {inserted} insertados, {updated} actualizados")

    export_rows = db.fetch_by_isbns(isbns)
    db.close()

    if args.output:
        out_path = Path(args.output)
        write_csv([_row_to_dict(r) for r in export_rows], out_path)
        print(f"[EXPORT] {len(export_rows)} filas -> {out_path}")
    else:
        print(json.dumps(export_rows[:5], ensure_ascii=False, indent=2))
        if len(export_rows) > 5:
            print(f"... ({len(export_rows)} filas exportables)")

    return 0

def cmd_export(args: argparse.Namespace) -> int:
    db = _get_db(args.db_path)

    if args.all:
        rows = db.fetch_all()
    else:
        if not (args.query or args.query_file):
            raise SystemExit("[ERROR] export requiere --all o --query/--query-file")
        if args.query:
            isbns = [normalize_isbn(args.query)]
        else:
            qf = resolve_query_file(args.query_file)
            isbns = load_isbns_from_query_file(qf, limit=args.limit_queries)
        rows = db.fetch_by_isbns(isbns)

    db.close()

    out_path = Path(args.output)
    write_csv([_row_to_dict(r) for r in rows], out_path)
    print(f"[EXPORT] {len(rows)} filas -> {out_path}")
    return 0

def cmd_clean(args: argparse.Namespace) -> int:
    inp = Path(args.input)
    if not inp.exists():
        raise SystemExit(f"[ERROR] No existe input: {inp}")
    outp = Path(args.output) if args.output else inp.with_name(inp.stem + "_clean.csv")

    with inp.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    strip_cols = []
    if args.strip_html_cols:
        strip_cols = [c.strip() for c in args.strip_html_cols.split(",") if c.strip()]

    flatten_col = args.flatten_json_col.strip() if args.flatten_json_col else ""

    extra_keys: Set[str] = set()
    for r in rows:
        for c in strip_cols:
            if c in r and r[c]:
                r[c] = strip_html(r[c])
        if flatten_col and flatten_col in r and r[flatten_col]:
            try:
                d = json.loads(r[flatten_col])
                if isinstance(d, dict):
                    for k, v in d.items():
                        kk = f"{flatten_col}__{k}"
                        if kk not in r:
                            r[kk] = v
                            extra_keys.add(kk)
            except Exception:
                pass

    out_fields = list(fieldnames)
    for k in sorted(extra_keys):
        if k not in out_fields:
            out_fields.append(k)

    with outp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"[CLEAN] {len(rows)} filas -> {outp}")
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", description="BookSearchV2 CLI maestro")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_sites = sub.add_parser("sites", help="Listar sitios disponibles")
    p_sites.set_defaults(func=cmd_sites)

    p_help = sub.add_parser("site-help", help="Ayuda de un sitio específico")
    p_help.add_argument("--site", required=True)
    p_help.set_defaults(func=cmd_site_help)

    # -----------------
    # run
    # -----------------
    p_run = sub.add_parser("run", help="Ejecutar un sitio (query o query-file)")
    p_run.add_argument("--site", required=True)
    p_run.add_argument(
        "--fetch-detail", "--fetch_detail",
        dest="fetch_detail",
        type=_str2bool,
        nargs="?",
        const=True,
        default=True,
        help="Si False, no pide la página de detalle (/bd)."
    )
    p_run.add_argument("--query", help="Consulta única (ISBN o título)")
    p_run.add_argument("--query-file", help="Archivo .txt con consultas")
    p_run.add_argument("--max-results", type=int, default=4)
    p_run.add_argument("--delay", type=float, default=2.0)
    p_run.add_argument("--query-delay", type=float, default=1.0)
    p_run.add_argument("--batch-size", type=int, default=0)
    p_run.add_argument("--batch-pause", type=float, default=0.0)
    p_run.add_argument("--limit-queries", type=int, default=0, help="0 = sin límite")
    p_run.add_argument("--output", help="Salida CSV")
    p_run.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_run.add_argument("--write-db", action="store_true", help="Upsert resultados en la DB")
    p_run.set_defaults(func=cmd_run)

    # -----------------
    # sync
    # -----------------
    p_sync = sub.add_parser("sync", help="DB-first: busca en DB, scrappea faltantes, exporta")
    p_sync.add_argument("--query", help="ISBN único")
    p_sync.add_argument(
        "--fetch-detail", "--fetch_detail",
        dest="fetch_detail",
        type=_str2bool,
        nargs="?",
        const=True,
        default=True,
        help="Si False, no pide la página de detalle (/bd)."
    )
    p_sync.add_argument("--query-file", help="Archivo .txt con ISBNs")
    p_sync.add_argument("--sites", nargs="*", default=[], help="Orden de sitios para buscar faltantes (ej: yenny_search bookpeople)")
    p_sync.add_argument("--site-params-file", default=str(_repo_root() / "data" / "site_params.json"), help="JSON con overrides por sitio (delay/query_delay/batch_size/batch_pause/max_results)")
    p_sync.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_sync.add_argument("--remaining-out", default="", help="Archivo .txt con ISBNs que siguen faltando al final del sync")
    p_sync.add_argument("--output", default="", help="Salida CSV final (exporta desde DB al terminar)")
    p_sync.add_argument("--limit-queries", type=int, default=0)
    p_sync.add_argument("--max-results", type=int, default=4)
    p_sync.add_argument("--delay", type=float, default=2.0)
    p_sync.add_argument("--query-delay", type=float, default=1.0)
    p_sync.add_argument("--batch-size", type=int, default=0)
    p_sync.add_argument("--batch-pause", type=float, default=0.0)
    p_sync.set_defaults(func=cmd_sync)

    # -----------------
    # export
    # -----------------
    p_export = sub.add_parser("export", help="Exportar desde DB")
    p_export.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_export.add_argument("--all", action="store_true", help="Exportar toda la tabla libros")
    p_export.add_argument("--query", help="ISBN único")
    p_export.add_argument("--query-file", help="Archivo .txt con ISBNs")
    p_export.add_argument("--limit-queries", type=int, default=0)
    p_export.add_argument("--output", required=True, help="Salida CSV")
    p_export.set_defaults(func=cmd_export)

    # -----------------
    # clean
    # -----------------
    p_clean = sub.add_parser("clean", help="Post-procesar un CSV (limpiar HTML / aplanar JSON)")
    p_clean.add_argument("--input", required=True, help="CSV de entrada")
    p_clean.add_argument("--output", help="CSV de salida (default: *_clean.csv)")
    p_clean.add_argument("--strip-html-cols", default="", help="Columnas a limpiar HTML (coma-separadas)")
    p_clean.add_argument("--flatten-json-col", default="", help="Columna JSON a aplanar (ej: info_adicional)")
    p_clean.set_defaults(func=cmd_clean)

    # -----------------
    # db
    # -----------------
    p_db = sub.add_parser("db", help="Herramientas de base de datos (QC report, etc.)")
    p_db.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_db.add_argument("--qc-report", action="store_true", help="Genera reporte QC desde book_std.raw_json")
    p_db.add_argument("--output", default="", help="Salida CSV reporte agregado (default: data/exports/qc_report.csv)")
    p_db.add_argument("--details", default="", help="Salida CSV con detalle (opcional)")
    p_db.add_argument("--details-limit", type=int, default=0, help="0 = sin límite (solo aplica a --details)")
    p_db.add_argument("--site", default="", help="Filtra por site (ej: yenny, bookpeople)")
    p_db.add_argument("--only-warn", action="store_true", help="Solo incluir severity=warn")
    p_db.add_argument("--min-count", type=int, default=1, help="Mínimo de ocurrencias para incluir en el agregado")
    p_db.set_defaults(func=cmd_db)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))

if __name__ == "__main__":
    raise SystemExit(main())