# app/standard.py
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit
from app.core.qc import qc_standard_row
import csv

STD_FIELDS = [
    "ISBN",
    "TITULO",
    "AUTOR",
    "EDITORIAL",
    "SINOPSIS",
    "IDIOMA",
    "PAGINAS",
    "DIMENSIONES",
    "FECHA PUBLICACION",
    "URL",
    "URL PORTADA",
    "ENCUADERNACION",
    "CATEGORIA",
    "SITE",

]

# Portadas "placeholder" conocidas (se exportan como vacío)
_PLACEHOLDER_IMAGE_SUFFIXES = (
    "sin_foto.jpg",
)

_LANG_MAP = {
    "espanol": "Español",
    "español": "Español",
    "castellano": "Español",
    "english": "Inglés",
    "ingles": "Inglés",
    "inglés": "Inglés",
    "portugues": "Portugués",
    "portugués": "Portugués",
    "portuguese": "Portugués",
}

_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")
_RE_INT = re.compile(r"(\d+)")
_RE_DATE_DMY = re.compile(r"^\s*(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})\s*$")
_RE_DATE_YMD = re.compile(r"^\s*(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})\s*$")
_RE_DIM_2NUM = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xX]\s*(\d+(?:[.,]\d+)?)")
_RE_HAS_UNIT = re.compile(r"\b(cm|mm|in|inch|pulgadas)\b", re.I)


def strip_html(text: Any) -> str:
    if text is None:
        return ""
    s = str(text)
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"</(p|div|li|tr|h1|h2|h3)\s*>", "\n", s, flags=re.I)
    s = _RE_TAGS.sub(" ", s)
    try:
        import html as _html
        s = _html.unescape(s)
    except Exception:
        pass
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = _RE_WS.sub(" ", s).strip()
    return s


def normalize_isbn(value: Any) -> str:
    if value is None:
        return ""
    # si viene float tipo 9788418184888.0
    if isinstance(value, float):
        if value.is_integer():
            value = int(value)
    s = str(value).strip().upper()
    s = re.sub(r"[^0-9X]", "", s)
    return s


def titleish(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).strip()
    if not t:
        return ""
    # si viene TODO en mayúsculas, lo bajamos a Title Case
    if t.isupper() and len(t) >= 4:
        return t.title()
    return t


def normalize_language(s: Any) -> str:
    t = titleish(s)
    if not t:
        return ""
    key = t.strip().lower()
    key = (
        key.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
    )
    key = re.sub(r"[^a-z]+", "", key)
    return _LANG_MAP.get(key, t.title() if t.isupper() else t)


def _strip_accents(s: str) -> str:
    # NFKD splits accents into combining marks; we drop those marks.
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )

def normalize_encuadernacion(value: Any) -> str:
    """
    Normaliza encuadernación a un set chico y consistente (español):
      - Paperback / Mass Market Paperback -> De Bolsillo
      - Tapa blanda / Rústica / Softcover / Trade Paperback -> Rústica
      - Hardcover / Tapa Dura -> Tapa Dura
      - eBook/Digital -> eBook
    """
    if value is None:
        return ""

    vv = str(value).strip()
    if vv in ("-", "–", "—", "N/A", "n/a", "NA", "na", "None", "null", "NULL", "s/d", "sin dato"):
        return ""

    # quitar sufijos tipo "Paperback (8/28/2006)"
    v2 = re.sub(r"\s*\([^)]*\)\s*", "", vv).strip()

    low = _strip_accents(v2).lower().strip()
    low = re.sub(r"\s+", " ", low)

    if any(x in low for x in ["ebook", "e-book", "digital", "kindle"]):
        return "eBook"

    if any(x in low for x in ["hardcover", "hard cover", "hardback", "hard-back", "tapa dura"]):
        return "Tapa Dura"

    # Paperback -> De Bolsillo
    if any(x in low for x in ["paperback", "mass market paperback"]):
        return "De Bolsillo"

    # Tapa blanda / rústica / trade paperback
    if any(x in low for x in ["trade paperback", "tapa blanda", "rustica", "rstica", "softcover", "soft cover"]):
        return "Rústica"

    # si ya viene bonito
    if low in ("tapa dura",):
        return "Tapa Dura"
    if low in ("rustica", "rstica"):
        return "Rústica"
    if low in ("de bolsillo", "bolsillo"):
        return "De Bolsillo"

    return v2


def parse_date_iso(s: Any) -> str:
    """
    Convierte:
      09/03/2022 -> 2022-03-09
      2022-03-09 -> 2022-03-09
    Si no puede, devuelve limpio o "".
    """
    t = titleish(s)
    if not t:
        return ""
    m = _RE_DATE_DMY.match(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _RE_DATE_YMD.match(t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return t


def format_pages(value: Any) -> Dict[str, Any]:
    """
    Devuelve:
      display: '224 págs'
      num: 224 (para info_adicional si querés)
    """
    if value is None:
        return {"display": "", "num": None}
    t = strip_html(value)
    if not t:
        return {"display": "", "num": None}
    m = _RE_INT.search(t)
    if not m:
        return {"display": t, "num": None}
    n = int(m.group(1))
    return {"display": f"{n} págs", "num": n}


def format_dimensions(value: Any) -> str:
    t = strip_html(value)
    if not t:
        return ""
    t = t.replace(" X ", " x ").replace(" x ", " x ")
    t = re.sub(r"\s*[xX]\s*", " x ", t)
    t = re.sub(r"\s+", " ", t).strip()

    m = _RE_DIM_2NUM.search(t)
    if m:
        a = m.group(1).replace(",", ".")
        b = m.group(2).replace(",", ".")
        base = f"{a} x {b}"
        if not _RE_HAS_UNIT.search(t):
            return base + " cm"
        # conserva unidad si ya está
        # (normaliza 'CM'->'cm')
        t2 = re.sub(r"\bCM\b", "cm", t, flags=re.I)
        # reemplaza la parte numérica por base
        return _RE_DIM_2NUM.sub(base, t2, count=1)
    return t


def _json_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        try:
            d = json.loads(x)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _pick(raw: Dict[str, Any], keys: List[str], extra: Dict[str, Any] | None = None) -> Any:
    for k in keys:
        if k in raw and raw.get(k) not in (None, "", []):
            return raw.get(k)
    if extra:
        for k in keys:
            if k in extra and extra.get(k) not in (None, "", []):
                return extra.get(k)
    return ""


def absolutize_url(url: str, maybe_relative: str) -> str:
    u = (url or "").strip()
    r = (maybe_relative or "").strip()
    if not r:
        return ""
    if r.startswith("http://") or r.startswith("https://"):
        return r
    if u:
        return urljoin(u, r)
    return r


def _is_placeholder_image(img_url: str) -> bool:
    if not img_url:
        return False
    try:
        path = urlsplit(img_url).path.lower().strip()
    except Exception:
        path = img_url.lower().strip()
    return any(path.endswith(suf) for suf in _PLACEHOLDER_IMAGE_SUFFIXES)


def to_standard_row(raw: Dict[str, Any], site_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Convierte cualquier row (scrape o DB) al schema estándar de 14 columnas (se mantiene compatible con el anterior).
    """
    extra = _json_dict(raw.get("info_adicional"))
    site = site_id or raw.get("SITE") or raw.get("site") or raw.get("sitio") or raw.get("source") or ""

    url = _pick(raw, ["URL", "url", "url_detalle", "detail_url", "link"], extra=None)
    portada = _pick(raw, ["URL PORTADA", "url_portada", "portada_url", "cover_url", "image", "image_url"], extra=None)
    portada = absolutize_url(str(url), str(portada))

    # ✅ limpiar placeholder de portada (sin_foto.jpg)
    if _is_placeholder_image(portada):
        portada = ""

    isbn = normalize_isbn(_pick(raw, ["ISBN", "isbn", "gtin13", "ean"], extra))
    titulo = titleish(_pick(raw, ["TITULO", "titulo", "title", "name"], extra))
    autor = titleish(_pick(raw, ["AUTOR", "autor", "author"], extra))
    editorial = titleish(_pick(raw, ["EDITORIAL", "editorial", "publisher"], extra))
    encuadernacion = titleish(strip_html(_pick(raw, ["ENCUADERNACION", "encuadernacion", "binding", "formato", "format", "cover", "tipo_encuadernacion", "tipo_de_encuadernacion"], extra)))
    categoria = titleish(strip_html(_pick(raw, ["CATEGORIA", "categoria", "categoría", "category", "genre", "genero", "género"], extra)))
    idioma = normalize_language(_pick(raw, ["IDIOMA", "idioma", "language"], extra))

    # sinopsis
    sinopsis = _pick(raw, ["SINOPSIS", "sinopsis", "descripcion", "description", "summary"], extra)
    sinopsis = strip_html(sinopsis)
    # limpieza típica (por si viene arrastrado)
    sinopsis = re.sub(r"^\s*Información adicional\s*Descripción\s*", "", sinopsis, flags=re.I).strip()

    # páginas
    pages_src = _pick(raw, ["PAGINAS", "paginas", "pages"], extra)
    pages = format_pages(pages_src)

    # dimensiones
    dim_src = _pick(raw, ["DIMENSIONES", "dimensiones", "medidas", "dimensions"], extra)
    dimensiones = format_dimensions(dim_src)

    # fecha publicación
    date_src = _pick(
        raw,
        ["FECHA PUBLICACION", "fecha_publicacion", "fecha_edicion", "published", "publication_date"],
        extra,
    )
    fecha_pub = parse_date_iso(date_src)

    out = {
        "ISBN": isbn,
        "TITULO": titulo,
        "AUTOR": autor,
        "EDITORIAL": editorial,
        "ENCUADERNACION": encuadernacion,
        "CATEGORIA": categoria,
        "SINOPSIS": sinopsis,
        "IDIOMA": idioma,
        "PAGINAS": pages["display"],
        "DIMENSIONES": dimensiones,
        "FECHA PUBLICACION": fecha_pub,
        "URL": str(url or ""),
        "URL PORTADA": str(portada or ""),
        "SITE": str(site or ""),
    }    
    # opcional: si querés conservar numérico para búsquedas internas, queda en info_adicional
    # (NO rompe el schema de salida, porque no agrega columna)
    if pages["num"] is not None:
        extra.setdefault("paginas_num", pages["num"])
        raw["info_adicional"] = extra

    out, _issues = qc_standard_row(out, mode="fix")
    return out

def standardize_rows(rows: List[Dict[str, Any]], site_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return [to_standard_row(r, site_id=site_id) for r in rows]


def write_standard_csv(rows: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STD_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)