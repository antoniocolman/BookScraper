from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

def _repo_root() -> Path:
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