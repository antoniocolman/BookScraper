from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import httpx

BASE = "https://www.amazon.com"
SEARCH_URL = BASE + "/s?k={q}&i=stripbooks"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# señales de bloqueo
_RE_ROBOT = re.compile(r"(Robot Check|captcha|Enter the characters you see)", re.I)

# JSON-LD
_RE_LDJSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)

# search
_RE_ASIN = re.compile(r'data-asin=["\']([A-Z0-9]{10})["\']', re.I)
_RE_DP = re.compile(r'href=["\'](/(?:-/[a-z]{2}/)?dp/[A-Z0-9]{10}[^"\']*)["\']', re.I)

# title / subtitle
_RE_TITLE = re.compile(r'id=["\']productTitle["\'][^>]*>\s*(.*?)\s*<', re.I | re.S)
_RE_SUBTITLE = re.compile(r'id=["\']productSubtitle["\'][^>]*>\s*(.*?)\s*<', re.I | re.S)

# images
_RE_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_RE_LANDING_HIRES = re.compile(
    r'id=["\']landingImage["\'][^>]*data-old-hires=["\']([^"\']+)["\']',
    re.I
)
_RE_LANDING_SRC = re.compile(
    r'id=["\']landingImage["\'][^>]*src=["\']([^"\']+)["\']',
    re.I
)
# fallback extra (este es el que preguntabas "dónde va": va con los regex de imágenes)
_RE_ANY_HIRES = re.compile(r'data-old-hires=["\']([^"\']+)["\']', re.I)

# detail bullets
_RE_DETAIL_DIV_ID = re.compile(r'id=["\']detailBullets_feature_div["\']', re.I)
_RE_UL_TOKEN = re.compile(r"<ul\b[^>]*>|</ul>", re.I)
_RE_LI = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_RE_LABEL = re.compile(
    r'<span[^>]*class=["\'][^"\']*\ba-text-bold\b[^"\']*["\'][^>]*>(.*?)</span>',
    re.I | re.S
)
_RE_NEXT_SPAN = re.compile(
    r"<span\b(?![^>]*\ba-text-bold\b)[^>]*>(.*?)</span>",
    re.I | re.S
)

_RE_DIV_TOKEN = re.compile(r"<div\b[^>]*>|</div>", re.I)
_RE_DESC_START = re.compile(
    r'<div\b[^>]*data-expanded=["\']true["\'][^>]*class=["\'][^"\']*a-expander-content[^"\']*["\'][^>]*>',
    re.I
)

def _extract_outer_div_from(html: str, start_idx: int) -> str:
    """
    Extrae el HTML completo de un <div> (incluyendo anidados) usando conteo de profundidad.
    start_idx debe apuntar al inicio del tag <div ...>.
    """
    if not html or start_idx < 0:
        return ""

    window = html[start_idx:start_idx + 250000]  # ventana suficiente
    m = re.match(r"<div\b[^>]*>", window, re.I)
    if not m:
        return ""

    depth = 0
    end = None

    for t in _RE_DIV_TOKEN.finditer(window, pos=0):
        tok = t.group(0).lower()
        if tok.startswith("<div"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                end = t.end()
                break

    if end is None:
        return ""
    return window[:end]

def _clean_multiline_text(s: str) -> str:
    s = (s or "").replace("\r", "")
    # normaliza espacios pero preserva saltos de línea
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

# ASIN from url
_RE_ASIN_FROM_URL = re.compile(r"/dp/([A-Z0-9]{10})", re.I)

# author fallback
_RE_AUTHOR_SPAN = re.compile(
    r'<span[^>]*class=["\'][^"\']*\bauthor\b[^"\']*["\'][^>]*>(.*?)</span>',
    re.I | re.S
)
_RE_BYLINE = re.compile(r'id=["\']bylineInfo["\'][^>]*>(.*?)</', re.I | re.S)
_RE_A_TEXT = re.compile(r'<a[^>]*class=["\']a-link-normal["\'][^>]*>(.*?)</a>', re.I | re.S)

# synopsis
_RE_BOOKDESC = re.compile(
    r'id=["\']bookDescription_feature_div["\'][\s\S]{0,12000}?<div[^>]*class=["\'][^"\']*a-expander-content[^"\']*["\'][^>]*>(.*?)</div>',
    re.I | re.S
)
_RE_DESC_FALLBACK = re.compile(
    r'<div[^>]*class=["\'][^"\']*a-expander-content[^"\']*["\'][^>]*data-expanded=["\']true["\'][^>]*>(.*?)</div>',
    re.I | re.S
)

# dates
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_RE_DATE_EN = re.compile(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b")
_RE_DATE_ES = re.compile(r"\b(\d{1,2})\s+de\s+([A-Za-záéíóúñ]+)\s+de\s+(\d{4})\b", re.I)
_RE_DATE_ES2 = re.compile(r"\b(\d{1,2})\s+([A-Za-záéíóúñ]+)\s+(\d{4})\b", re.I)


@dataclass(frozen=True)
class QueryItem:
    raw: str
    isbn: str


class BlockedByAmazon(RuntimeError):
    pass


def _strip_tags(x: str) -> str:
    return re.sub(r"<[^>]+>", "", x or "")


def _clean_text(s: str) -> str:
    s = (s or "")
    for ch in ["\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\ufeff"]:
        s = s.replace(ch, "")
    s = re.sub(r"\s+", " ", s.strip())
    try:
        import html as _html
        s = _html.unescape(s)
    except Exception:
        pass
    return s.strip()


def normalize_isbn(value: str) -> str:
    s = (value or "").strip().upper()
    s = re.sub(r"[^0-9X]", "", s)
    return s


def parse_query_line(line: str) -> Optional[QueryItem]:
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    token = re.split(r"[\t;|, ]", raw, maxsplit=1)[0].strip()
    isbn = normalize_isbn(token)
    if len(isbn) not in (10, 13):
        return None
    return QueryItem(raw=raw, isbn=isbn)


def load_queries_from_file(path: str, limit: Optional[int] = None) -> List[QueryItem]:
    out: List[QueryItem] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            q = parse_query_line(ln)
            if q:
                out.append(q)
                if limit and len(out) >= limit:
                    break
    seen = set()
    uniq: List[QueryItem] = []
    for q in out:
        if q.isbn not in seen:
            seen.add(q.isbn)
            uniq.append(q)
    return uniq


def make_session() -> httpx.Client:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Connection": "keep-alive",
    }
    return httpx.Client(headers=headers, follow_redirects=True, timeout=30.0)


def _fetch_html(session: httpx.Client, url: str, *, max_retries: int, base_wait: float) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            r = session.get(url)
            if r.status_code in (429, 503):
                txt = r.text or ""
                if _RE_ROBOT.search(txt):
                    raise BlockedByAmazon(f"Bloqueo/RobotCheck detectado en {url} (status={r.status_code})")
                raise httpx.HTTPStatusError("rate/503", request=r.request, response=r)
            r.raise_for_status()
            txt = r.text or ""
            if _RE_ROBOT.search(txt):
                raise BlockedByAmazon(f"Bloqueo/RobotCheck detectado en {url}")
            return txt
        except BlockedByAmazon:
            raise
        except Exception as e:
            last_exc = e
            if attempt >= max_retries:
                break
            sleep_s = min(180.0, (base_wait * (2 ** attempt))) + random.uniform(0, 1.5)
            time.sleep(sleep_s)
    raise last_exc or RuntimeError("fetch failed")


def _pick_first_product_url_from_search(html: str) -> Optional[str]:
    m = _RE_DP.search(html or "")
    if m:
        return urljoin(BASE, m.group(1))
    m2 = _RE_ASIN.search(html or "")
    if m2:
        asin = m2.group(1)
        return f"{BASE}/dp/{asin}"
    return None


def _extract_ld_book(html: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for blob in _RE_LDJSON.findall(html or ""):
        blob = (blob or "").strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue

        candidates: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates += [x for x in data["@graph"] if isinstance(x, dict)]
            candidates.append(data)
        elif isinstance(data, list):
            candidates += [x for x in data if isinstance(x, dict)]

        for obj in candidates:
            typ = obj.get("@type")
            if isinstance(typ, list):
                typ_ok = any(str(t).lower() == "book" for t in typ)
            else:
                typ_ok = str(typ or "").lower() == "book"
            if not typ_ok:
                continue

            out["title"] = out.get("title") or obj.get("name") or ""
            out["image"] = out.get("image") or obj.get("image") or ""
            out["date_published"] = out.get("date_published") or obj.get("datePublished") or ""
            out["isbn"] = out.get("isbn") or obj.get("isbn") or obj.get("isbn13") or ""

            pub = obj.get("publisher")
            if isinstance(pub, dict):
                out["publisher"] = out.get("publisher") or pub.get("name") or ""
            elif isinstance(pub, str):
                out["publisher"] = out.get("publisher") or pub

            auth = obj.get("author")
            if isinstance(auth, list):
                names = []
                for a in auth:
                    if isinstance(a, dict) and a.get("name"):
                        names.append(str(a["name"]))
                    elif isinstance(a, str):
                        names.append(a)
                if names:
                    out["author"] = out.get("author") or ", ".join(names)
            elif isinstance(auth, dict) and auth.get("name"):
                out["author"] = out.get("author") or str(auth["name"])
            elif isinstance(auth, str):
                out["author"] = out.get("author") or auth

    for k in list(out.keys()):
        if isinstance(out[k], str):
            out[k] = _clean_text(out[k])
    return out


def _extract_outer_ul_after_detail_div(html: str) -> str:
    """
    Devuelve el HTML del <ul> EXTERNO dentro de detailBullets_feature_div
    manejando <ul> anidados (conteo de profundidad).
    """
    if not html:
        return ""

    low = html.lower()
    idx = low.find('id="detailbullets_feature_div"')
    if idx == -1:
        idx = low.find("id='detailbullets_feature_div'")
    if idx == -1:
        return ""

    window = html[idx: idx + 250000]  # suficiente para capturar el bloque de detalles

    m = re.search(r"<ul\b[^>]*>", window, re.I)
    if not m:
        return ""

    start = m.start()
    depth = 0
    end = None

    for t in _RE_UL_TOKEN.finditer(window, pos=start):
        tok = t.group(0).lower()
        if tok.startswith("<ul"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                end = t.end()
                break

    if end is None:
        return ""

    return window[start:end]


def _extract_detail_bullets(html: str) -> Dict[str, str]:
    kv: Dict[str, str] = {}

    ul_block = _extract_outer_ul_after_detail_div(html)
    if not ul_block:
        return kv

    for li_body in _RE_LI.findall(ul_block):
        lm = _RE_LABEL.search(li_body)
        if not lm:
            continue

        label = _clean_text(_strip_tags(lm.group(1))).rstrip(":").strip()

        after = li_body[lm.end():]
        vm = _RE_NEXT_SPAN.search(after)
        if not vm:
            continue

        value = _clean_text(_strip_tags(vm.group(1))).strip()

        if label and value:
            kv[label] = value

    return kv


def _normalize_key(k: str) -> str:
    t = (k or "").strip().lower()
    t = (
        t.replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    )
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _pick_from_kv(kv: Dict[str, str], keys: List[str]) -> str:
    norm = {_normalize_key(k): v for k, v in kv.items()}
    for key in keys:
        k2 = _normalize_key(key)
        if k2 in norm and norm[k2]:
            return norm[k2]
    return ""


def _parse_pub_date_to_iso(s: str) -> str:
    t = _clean_text(s)
    if not t:
        return ""

    m = _RE_DATE_EN.search(t)
    if m:
        mon = _MONTHS_EN.get(m.group(1).strip().lower())
        day = int(m.group(2))
        year = int(m.group(3))
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"

    m = _RE_DATE_ES.search(t)
    if m:
        day = int(m.group(1))
        mon = _MONTHS_ES.get(m.group(2).strip().lower())
        year = int(m.group(3))
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"

    m = _RE_DATE_ES2.search(t)
    if m:
        day = int(m.group(1))
        mon = _MONTHS_ES.get(m.group(2).strip().lower())
        year = int(m.group(3))
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"

    return t


def _extract_cover_url(html: str) -> str:
    m = _RE_OG_IMAGE.search(html or "")
    if m:
        return m.group(1).strip()
    m = _RE_LANDING_HIRES.search(html or "")
    if m:
        return m.group(1).strip()
    m = _RE_ANY_HIRES.search(html or "")
    if m:
        return m.group(1).strip()
    m = _RE_LANDING_SRC.search(html or "")
    if m:
        return m.group(1).strip()
    return ""


def _extract_title(html: str) -> str:
    m = _RE_TITLE.search(html or "")
    if not m:
        return ""
    return _clean_text(_strip_tags(m.group(1)))


def _extract_binding_from_subtitle(html: str) -> str:
    m = _RE_SUBTITLE.search(html or "")
    if not m:
        return ""
    txt = _clean_text(_strip_tags(m.group(1)))
    for sep in [" – ", " - ", " — "]:
        if sep in txt:
            return txt.split(sep, 1)[0].strip()
    return txt.strip()


def _extract_synopsis(html: str) -> str:
    """
    Extrae la sinopsis desde #bookDescription_feature_div (expander content).
    Maneja divs anidados (conteo de profundidad) para no cortar mal.
    """
    if not html:
        return ""

    low = html.lower()
    idx = low.find('id="bookdescription_feature_div"')
    if idx == -1:
        idx = low.find("id='bookdescription_feature_div'")
    if idx == -1:
        return ""

    # Tomamos una ventana grande desde el div
    window = html[idx: idx + 250000]

    # Encontrar el comienzo del div "a-expander-content" expandido
    m = _RE_DESC_START.search(window)
    if not m:
        return ""

    # start dentro del HTML original
    start_in_window = m.start()
    start_in_html = idx + start_in_window

    outer = _extract_outer_div_from(html, start_in_html)
    if not outer:
        return ""

    # Nos quedamos solo con el contenido interno del div (sacamos tag inicial y final)
    inner = re.sub(r"^<div\b[^>]*>", "", outer, flags=re.I).strip()
    inner = re.sub(r"</div>\s*$", "", inner, flags=re.I).strip()

    # Preservar párrafos
    inner = inner.replace("</p>", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    inner = inner.replace("</li>", "\n")

    txt = _strip_tags(inner)
    txt = _clean_multiline_text(txt)
    # Por si quedaron espacios raros
    txt = _clean_text(txt).replace(" \n", "\n").replace("\n ", "\n")
    return txt

def _extract_authors(html: str) -> List[str]:
    authors: List[str] = []

    for block in _RE_AUTHOR_SPAN.findall(html or ""):
        for a in _RE_A_TEXT.findall(block or ""):
            name = _clean_text(_strip_tags(a))
            if name:
                authors.append(name)

    if not authors:
        m = _RE_BYLINE.search(html or "")
        if m:
            block = m.group(1)
            for a in _RE_A_TEXT.findall(block or ""):
                name = _clean_text(_strip_tags(a))
                if name:
                    authors.append(name)

    seen = set()
    uniq: List[str] = []
    for a in authors:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def canonicalize_url(u: str) -> str:
    try:
        p = urlsplit(u)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except Exception:
        return u


def _extract_asin(url: str, html: str) -> str:
    m = _RE_ASIN_FROM_URL.search(url or "")
    if m:
        return m.group(1).upper()
    m2 = _RE_ASIN.search(html or "")
    if m2:
        return m2.group(1).upper()
    return ""


def parse_product_page(html: str, url: str, isbn_hint: str = "") -> Dict[str, Any]:
    ld = _extract_ld_book(html)
    kv = _extract_detail_bullets(html)

    titulo = ld.get("title") or _extract_title(html)

    autores = _extract_authors(html)
    autor = (ld.get("author") or "").strip() or (autores[0] if autores else "")

    editorial = ld.get("publisher") or _pick_from_kv(kv, ["Editorial", "Publisher", "Imprint"])
    idioma = _pick_from_kv(kv, ["Idioma", "Language"])
    dimensiones = _pick_from_kv(kv, ["Dimensiones", "Dimensions"])

    paginas = _pick_from_kv(kv, ["Número de páginas", "Number of pages", "Print length"])
    fecha_pub_raw = ld.get("date_published") or _pick_from_kv(kv, ["Fecha de publicación", "Publication date"])
    fecha_pub = _parse_pub_date_to_iso(fecha_pub_raw)

    portada = ld.get("image") or _extract_cover_url(html)
    binding = _extract_binding_from_subtitle(html)

    isbn10 = normalize_isbn(_pick_from_kv(kv, ["ISBN-10", "ISBN10"]))
    isbn13 = normalize_isbn(_pick_from_kv(kv, ["ISBN-13", "ISBN13"]))

    if not isbn13:
        hint = normalize_isbn(isbn_hint)
        if len(hint) == 13:
            isbn13 = hint
        else:
            ld_isbn = normalize_isbn(ld.get("isbn") or "")
            if len(ld_isbn) == 13:
                isbn13 = ld_isbn

    asin = _extract_asin(url, html)
    sinopsis = _extract_synopsis(html)

    row: Dict[str, Any] = {
        "ISBN": isbn13 or normalize_isbn(isbn_hint) or normalize_isbn(ld.get("isbn") or "") or isbn10,
        "TITULO": titulo,
        "AUTOR": autor,
        "EDITORIAL": editorial,
        "SINOPSIS": sinopsis,
        "IDIOMA": idioma,
        "PAGINAS": paginas,
        "DIMENSIONES": dimensiones,
        "FECHA PUBLICACION": fecha_pub,
        "URL": canonicalize_url(url),
        "URL PORTADA": portada,
        "SITE": "amazon",

        "ISBN10": isbn10,
        "ENCUADERNACION": binding,
        "ASIN": asin,
        "_amazon_detail_kv": kv,
        "_amazon_authors": autores,
    }
    return row


def search_amazon_books(
    session: httpx.Client,
    q: QueryItem,
    *,
    delay: float = 1.0,
    max_retries: int = 2,
    base_wait: float = 12.0,
) -> Optional[Dict[str, Any]]:
    search_url = SEARCH_URL.format(q=quote_plus(q.isbn))
    sh = _fetch_html(session, search_url, max_retries=max_retries, base_wait=base_wait)
    if delay:
        time.sleep(delay)

    prod_url = _pick_first_product_url_from_search(sh)
    if not prod_url:
        return None

    ph = _fetch_html(session, prod_url, max_retries=max_retries, base_wait=base_wait)
    row = parse_product_page(ph, prod_url, isbn_hint=q.isbn)

    if not (row.get("TITULO") or "").strip():
        return None

    return row
