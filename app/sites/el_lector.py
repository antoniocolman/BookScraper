from __future__ import annotations

"""
El Lector - scraper estable (sin selectolax).
Parsea campos con selectores del HTML real.

Campos top-level (consistentes):
  url_detalle, titulo, autor, isbn, precio, descripcion (sinopsis), portada_url, info_adicional

info_adicional (dict) siempre intenta incluir:
  moneda, formato, tapa, encuadernacion, idioma, editorial, paginas, fecha_edicion, medidas
"""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin
import html as _html
import httpx
import re
import time
import difflib

SITE_ID = "el_lector"
SITE_NAME = "El Lector"
CAPABILITIES = ["query", "query-file", "url"]

BASE = "https://www.ellector.com.py"
SEARCH_URL = BASE + "/productos/buscar.html?buscando={q}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------- helpers (sin parser nativo) ----------

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

def _strip_tags(s: str) -> str:
    s = s or ""
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    s = re.sub(r"</(p|div|li|tr|h1|h2|h3)\s*>", "\n", s, flags=re.I)
    s = _TAGS.sub(" ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = _WS.sub(" ", s).strip()
    return s

def _normalize_isbn(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[^0-9Xx]", "", s).upper()

def _parse_price_pyg(raw: str) -> Optional[int]:
    """
    ₲170.000 -> 170000
    """
    if not raw:
        return None
    x = raw.strip()
    x = x.replace(".", "")
    x = x.split(",")[0]
    x = re.sub(r"[^0-9]", "", x)
    if not x:
        return None
    try:
        return int(x)
    except ValueError:
        return None

def _score_match(query: str, titulo: str, isbn: str) -> float:
    q = (query or "").strip()
    if not q:
        return 0.0
    qn = _normalize_isbn(q)
    isn = _normalize_isbn(isbn or "")

    if len(qn) in (10, 13):  # query es ISBN
        if isn == qn:
            return 10.0
        if qn and qn in isn:
            return 8.0
        return 0.0

    # query es texto
    if not titulo:
        return 0.0
    return difflib.SequenceMatcher(None, q.lower(), titulo.lower()).ratio()


def _match_reason(query: str, row: Dict[str, Any], score: float) -> str:
    qn = _normalize_isbn(query or "")
    rn = _normalize_isbn(str(row.get("isbn") or ""))
    if len(qn) in (10, 13):
        if rn == qn:
            return "ISBN exacto"
        if qn and qn in rn:
            return "ISBN parcial"
        return "ISBN no coincide"
    # query texto
    return f"similitud título={score:.3f}"

def _bool_str(x: Any) -> str:
    return "True" if bool(x) else "False"

# ---------- regex basados en TU HTML ----------

# producto: SOLO urls con "-id-" y sin "/categorias/"
_RE_PROD_REL = re.compile(r'href=["\'](/productos/(?!categorias/)[^"\']*?-id-[^"\']+?\.html)["\']', re.I)
_RE_PROD_ABS = re.compile(r'https?://(?:www\.)?ellector\.com\.py/productos/(?!categorias/)[^"\'\s<>]*?-id-[^"\'\s<>]+?\.html', re.I)

_RE_COVER = re.compile(r'<img[^>]*class=["\'][^"\']*\bw-100\b[^"\']*["\'][^>]*src=["\']([^"\']+)["\']', re.I)
_RE_H1 = re.compile(r'<h1[^>]*class=["\'][^"\']*\bproduct_title\b[^"\']*["\'][^>]*>(.*?)</h1>', re.I | re.S)
_RE_AUTHOR = re.compile(r'<div[^>]*class=["\'][^"\']*\bproduct-brand\b[^"\']*["\'][^>]*>.*?Autor:\s*<a[^>]*>(.*?)</a>', re.I | re.S)
_RE_ISBN = re.compile(r'<span[^>]*class=["\'][^"\']*\bsku\b[^"\']*["\'][^>]*>(.*?)</span>', re.I | re.S)

_RE_FORMAT_DIV = re.compile(r'<div[^>]*class=["\'][^"\']*\bformat\b[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S)
_RE_VALUE_SPAN = re.compile(r'<span[^>]*class=["\'][^"\']*\bvalue\b[^"\']*["\'][^>]*>(.*?)</span>', re.I | re.S)

_RE_PRICE = re.compile(r'<div[^>]*class=["\'][^"\']*\bprice\b[^"\']*["\'][^>]*>.*?<bdi[^>]*>.*?(?:currency-symbol[^>]*>\s*([^<\s]+)\s*<)?\s*([0-9][0-9\.\,]+)\s*<', re.I | re.S)

_RE_DESC_P = re.compile(r'<p[^>]*class=["\'][^"\']*\bproduct-description-text\b[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S)

_RE_TABLE = re.compile(r'<table[^>]*class=["\'][^"\']*\btable\b[^"\']*\btable-bordered\b[^"\']*["\'][^>]*>(.*?)</table>', re.I | re.S)
_RE_TR = re.compile(r"<tr>\s*<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>\s*</tr>", re.I | re.S)

_LABEL_MAP = {
    "fecha de edición": "fecha_edicion",
    "páginas": "paginas",
    "paginas": "paginas",
    "medidas": "medidas",
    "idioma": "idioma",
    "editorial": "editorial",
}

def _fetch(client: httpx.Client, url: str, timeout: float) -> str:
    r = client.get(url, follow_redirects=True, timeout=timeout)
    r.raise_for_status()
    return r.text

def _unique_keep_order(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _extract_candidate_urls(search_html: str) -> List[str]:
    urls: List[str] = []
    urls += _RE_PROD_ABS.findall(search_html)
    rels = _RE_PROD_REL.findall(search_html)
    urls += [urljoin(BASE, r) for r in rels]
    return _unique_keep_order(urls)

def _split_formato(formato_raw: str) -> Tuple[str, str, str]:
    """
    Formato "Tapa Blanda - Rústica" ->
      formato="Tapa Blanda - Rústica"
      tapa="Blanda"
      encuadernacion="Rústica"
    Heurístico, pero consistente.
    """
    formato = (formato_raw or "").strip()
    tapa = ""
    enc = ""

    if formato:
        # tapa: si empieza con "Tapa ..."
        m = re.match(r"(?i)\s*tapa\s+([^-]+)", formato)
        if m:
            tapa = m.group(1).strip()
        # encuadernación: si hay guión
        if "-" in formato:
            parts = [p.strip() for p in formato.split("-", 1)]
            if len(parts) == 2 and parts[1]:
                enc = parts[1]

    return formato, tapa, enc

def _parse_additional_table(html: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    m = _RE_TABLE.search(html)
    if not m:
        return info

    table_html = m.group(1)
    for th, td in _RE_TR.findall(table_html):
        k = _strip_tags(th).strip().lower()
        v = _strip_tags(td).strip()
        if not k or not v:
            continue
        key = _LABEL_MAP.get(k, k.replace(" ", "_"))
        info[key] = v
    return info

def _parse_product_page(html: str, url: str) -> Dict[str, Any]:
    # portada
    portada = ""
    m = _RE_COVER.search(html)
    if m:
        src = (m.group(1) or "").strip()
        if src:
            portada = urljoin(BASE, src)

    # placeholder sin foto
    if portada and (portada.lower().endswith("/sin_foto.jpg") or portada.lower().endswith("sin_foto.jpg")):
        portada = ""

    # titulo
    titulo = ""
    m = _RE_H1.search(html)
    if m:
        titulo = _strip_tags(m.group(1))

    # autor
    autor = ""
    m = _RE_AUTHOR.search(html)
    if m:
        autor = _strip_tags(m.group(1))

    # isbn
    isbn = ""
    m = _RE_ISBN.search(html)
    if m:
        isbn = _normalize_isbn(_strip_tags(m.group(1)))

    # formato + idioma
    formato = ""
    tapa = ""
    encuadernacion = ""
    idioma = ""
    m = _RE_FORMAT_DIV.search(html)
    if m:
        div = m.group(1)
        values = [_strip_tags(x) for x in _RE_VALUE_SPAN.findall(div)]
        if values:
            formato, tapa, encuadernacion = _split_formato(values[0])
        if len(values) >= 2:
            idioma = values[1].strip()

    # precio
    precio = None
    moneda = "PYG"
    m = _RE_PRICE.search(html)
    if m:
        sym = (m.group(1) or "").strip()
        raw = (m.group(2) or "").strip()
        # si detectamos símbolo, lo podemos usar; si no, PYG igual
        if sym == "$":
            moneda = "USD"
        elif sym in ("₲", "Gs", "Gs."):
            moneda = "PYG"
        precio = _parse_price_pyg(raw) if moneda == "PYG" else None

    # sinopsis (párrafos)
    desc_parts = [_strip_tags(p) for p in _RE_DESC_P.findall(html)]
    desc_parts = [p for p in desc_parts if p]
    sinopsis = "\n\n".join(desc_parts).strip()

    # tabla adicional
    add = _parse_additional_table(html)

    # completar idioma/editorial/paginas/fecha/medidas desde tabla si faltan
    if not idioma and add.get("idioma"):
        idioma = str(add["idioma"]).title()
    editorial = str(add.get("editorial") or "").title() if add.get("editorial") else ""
    paginas = str(add.get("paginas") or "")
    fecha_edicion = str(add.get("fecha_edicion") or "")
    medidas = str(add.get("medidas") or "")

    info_adicional: Dict[str, Any] = {
        "moneda": moneda,
        "formato": formato,
        "tapa": tapa,
        "encuadernacion": encuadernacion,
        "idioma": idioma,
        "editorial": editorial,
        "paginas": paginas,
        "fecha_edicion": fecha_edicion,
        "medidas": medidas,
    }
    # también guardar el dict crudo de tabla (por si trae más campos en el futuro)
    if add:
        info_adicional["_tabla_adicional"] = add

    return {
        "url_detalle": url,
        "titulo": titulo,
        "autor": autor,
        "isbn": isbn,
        "precio": precio,
        "descripcion": sinopsis,        # <-- acá va la sinopsis limpia
        "info_adicional": info_adicional,
        "portada_url": portada,
    }

def run_single(
    query: str,
    *,
    max_results: int = 4,
    delay: float = 0.8,
    timeout: float = 25.0,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    headers = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}

    with httpx.Client(headers=headers) as client:
        # si viene URL directo
        if q.lower().startswith("http"):
            print(f"[DIRECT] {q}")
            html = _fetch(client, q, timeout)
            row = _parse_product_page(html, q)
            # si falta algo clave, igual devolvemos lo parseado (sirve para debug)
            if not row.get("titulo") or not row.get("isbn"):
                print("[WARN] URL directa parseada, pero faltan campos clave (titulo/isbn).")
            else:
                print("[INFO] Coincidencia directa por URL.")
            return [row]

        # buscar
        search_url = SEARCH_URL.format(q=quote_plus(q))
        print(f"[SEARCH] '{q}' -> {search_url}")
        sh = _fetch(client, search_url, timeout)
        candidates = _extract_candidate_urls(sh)

        if not candidates:
            print(f"[INFO] Ningún resultado en búsqueda para '{q}'.")
            return []

        # listar algunas URLs para que quede trazabilidad en consola
        show_n = min(5, len(candidates))
        for i, u in enumerate(candidates[:show_n], 1):
            print(f"  [PRODUCTO {i}] {u}")
        if len(candidates) > show_n:
            print(f"  ... (+{len(candidates) - show_n} más)")


        # tomamos un pool razonable de candidatos
        candidates = candidates[: max(12, max_results * 6)]

        rows: List[Tuple[float, Dict[str, Any]]] = []
        for i, url in enumerate(candidates, start=1):
            if delay and i > 1:
                time.sleep(delay)

            try:
                ph = _fetch(client, url, timeout)
                row = _parse_product_page(ph, url)

                # score / filtro
                score = _score_match(q, str(row.get("titulo") or ""), str(row.get("isbn") or ""))
                if len(_normalize_isbn(q)) in (10, 13) and score < 8.0:
                    continue

                rows.append((score, row))
                print(
                    f"    [CANDIDATO] ISBN={row.get('isbn','')} | "
                    f"Título='{row.get('titulo','')}' | score={score:.3f} "
                    f"({_match_reason(q, row, score)})"
                )
            except Exception as e:
                print(f"  [ERROR] No se pudo parsear {url}: {e}")
                continue

        if not rows:
            print(f"[INFO] Ningún producto válido para '{q}'.")
            return []

        rows.sort(key=lambda t: t[0], reverse=True)

        # resumen (compacto)
        best = rows[0][1]
        ia = best.get("info_adicional") or {}
        enc = ia.get("tapa") or ia.get("encuadernacion") or ""
        best_url = best.get("url_detalle") or best.get("url") or ""
        print(
            f"[MATCH] ISBN={best.get('isbn','')} | "
            f"Título='{best.get('titulo','')}' | "
            f"Autor='{best.get('autor','')}' | "
            f"Enc='{enc}' | "
            f"URL={best_url}"
        )

        return [r for _, r in rows[:max_results]]

def run_from_file(
    query_file: str,
    *,
    limit_queries: Optional[int] = None,
    max_results: int = 4,
    delay: float = 0.8,
    query_delay: float = 0.0,
    batch_size: int = 0,
    batch_pause: float = 0.0,
    timeout: float = 25.0,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def iter_lines(path: str):
        n = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                q = raw.split("\t", 1)[0].strip()
                q = q.split("|", 1)[0].strip()
                if not q:
                    continue
                n += 1
                if limit_queries and n > limit_queries:
                    break
                yield n, q

    batch_count = 0
    for idx, q in iter_lines(query_file):
        if query_delay and idx > 1:
            time.sleep(query_delay)

        print("=" * 80)
        print(f"[ElLector][QUERY #{idx}] {q}")

        rows = run_single(q, max_results=max_results, delay=delay, timeout=timeout, **kwargs)
        out.extend(rows)

        batch_count += 1
        if batch_size and batch_count >= batch_size:
            if batch_pause:
                time.sleep(batch_pause)
            batch_count = 0

    return out