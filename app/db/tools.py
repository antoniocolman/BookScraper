from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple, Optional
from app.utils.common import normalize_isbn

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