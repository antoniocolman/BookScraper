# app/qc.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

# Stopwords mínimas ES vs EN (heurística liviana)
_STOP_ES = {
    "el","la","los","las","de","del","y","o","en","para","por","con","sin",
    "un","una","al","a","que","como","cuando","donde","quien","su","sus","se","es","son",
}
_STOP_EN = {
    "the","and","or","of","to","in","for","with","without","on","at","from","by",
    "a","an","is","are","was","were","this","that","these","those",
}

# patrones típicos de “imagen genérica/placeholder”
_COVER_BAD_PATTERNS = (
    "sin_foto", "placeholder", "no-image", "noimage", "not-available",
    "image-not-available", "default", "coming-soon", "comingsoon",
    "missing", "blank", "generic", "cover-not-available",
)

_RE_WORDS = re.compile(r"[a-záéíóúñ]+", re.I)
_RE_ACCENTS = re.compile(r"[áéíóúñ]", re.I)
_RE_ISBN_IN_URL = re.compile(r"(?<!\d)(\d{10,13})(?!\d)")


@dataclass(frozen=True)
class QCIssue:
    code: str       # e.g. LANG_MISMATCH, COVER_PLACEHOLDER
    severity: str   # "info" | "warn"
    message: str


def _clean_text(x: Any) -> str:
    if x is None:
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()


def guess_lang_es_en(text: str) -> Tuple[str, float]:
    """
    Heurística rápida ES vs EN.
    Devuelve ("Español"/"Inglés"/"", conf 0..1)
    """
    t = _clean_text(text).lower()
    if not t:
        return "", 0.0

    words = _RE_WORDS.findall(t)
    if not words:
        return "", 0.0

    es = sum(1 for w in words if w in _STOP_ES)
    en = sum(1 for w in words if w in _STOP_EN)
    accents = len(_RE_ACCENTS.findall(t))

    es_score = es + min(3, accents) * 0.75
    en_score = en

    if es_score < 2 and en_score < 2:
        return "", 0.0
    if es_score == en_score:
        return "", 0.0

    lang = "Español" if es_score > en_score else "Inglés"
    conf = abs(es_score - en_score) / max(es_score, en_score, 1.0)
    conf = max(0.0, min(1.0, float(conf)))
    return lang, conf


def is_generic_cover(url: str) -> Tuple[bool, str]:
    u = _clean_text(url).lower()
    if not u:
        return False, ""

    if u.startswith("data:"):
        return True, "data_uri"

    try:
        path = (urlsplit(u).path or "").lower()
    except Exception:
        path = u

    for p in _COVER_BAD_PATTERNS:
        if p in u or p in path:
            return True, f"pattern:{p}"

    # si el nombre de archivo no tiene extensión, suele ser raro
    last = path.split("/")[-1]
    if last and "." not in last:
        return True, "no_extension"

    return False, ""


def qc_standard_row(
    row: Dict[str, Any],
    *,
    mode: str = "fix",          # "fix" o "flag"
    lang_fix_conf: float = 0.75,
    lang_warn_conf: float = 0.55,
    clear_cover_on_isbn_mismatch: bool = True,
) -> Tuple[Dict[str, Any], List[QCIssue]]:
    """
    Sanitiza una fila estándar (claves tipo 'ISBN','TITULO','IDIOMA','URL PORTADA',...).
    - Idioma: marca mismatch ES/EN y (si mode='fix' y conf alta) corrige/llena 'IDIOMA'
    - Portada: limpia placeholders y (si detecta ISBN distinto en la URL) vacía la portada
    Devuelve (row_sanitizada, issues)
    """
    out = dict(row)
    issues: List[QCIssue] = []

    isbn = _clean_text(out.get("ISBN") or "")
    titulo = _clean_text(out.get("TITULO") or "")
    sinopsis = _clean_text(out.get("SINOPSIS") or "")
    idioma = _clean_text(out.get("IDIOMA") or "")
    cover = _clean_text(out.get("URL PORTADA") or "")

    # ---- Idioma: ES/EN mismatch
    pred, conf = guess_lang_es_en(f"{titulo}. {sinopsis}")
    if pred and conf >= lang_warn_conf and idioma and idioma != pred:
        issues.append(QCIssue("LANG_MISMATCH", "warn", f"IDIOMA='{idioma}' pero parece '{pred}' (conf≈{conf:.2f})"))
        if mode == "fix" and conf >= lang_fix_conf:
            out["IDIOMA"] = pred
            issues.append(QCIssue("LANG_FIXED", "info", f"IDIOMA corregido a '{pred}'"))

    if mode == "fix" and (not idioma) and pred and conf >= lang_fix_conf:
        out["IDIOMA"] = pred
        issues.append(QCIssue("LANG_FILLED", "info", f"IDIOMA completado a '{pred}'"))

    # ---- Cover: placeholder/genérica
    if cover:
        bad, why = is_generic_cover(cover)
        if bad:
            issues.append(QCIssue("COVER_PLACEHOLDER", "warn", f"Portada genérica ({why})"))
            if mode == "fix":
                out["URL PORTADA"] = ""
                issues.append(QCIssue("COVER_CLEARED", "info", "URL PORTADA vaciada (placeholder)"))

    # ---- Cover: ISBN incrustado en URL y no coincide -> casi seguro incorrecta
    if cover and isbn:
        m = _RE_ISBN_IN_URL.search(cover)
        if m:
            isbn_in_url = m.group(1)
            if isbn_in_url and isbn_in_url != isbn:
                issues.append(QCIssue("COVER_ISBN_MISMATCH", "warn", f"URL PORTADA contiene ISBN {isbn_in_url} != {isbn}"))
                if mode == "fix" and clear_cover_on_isbn_mismatch:
                    out["URL PORTADA"] = ""
                    issues.append(QCIssue("COVER_CLEARED", "info", "URL PORTADA vaciada (ISBN mismatch)"))

    return out, issues
