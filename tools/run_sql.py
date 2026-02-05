from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple


def read_sql(path: Path) -> str:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    return txt.lstrip("\ufeff")


def split_statements(sql: str) -> List[str]:
    parts = [p.strip() for p in sql.split(";")]
    return [p for p in parts if p]


def is_result_query(stmt: str) -> bool:
    s = stmt.strip().lower()
    return s.startswith("select") or s.startswith("pragma") or s.startswith("with")


def print_rows(cols: Sequence[str], rows: Sequence[Tuple[Any, ...]], limit: int = 200) -> None:
    rows = list(rows)
    show = rows if limit == 0 else rows[:limit]
    print("COLUMNS:", list(cols))
    for r in show:
        print(r)
    if limit > 0 and len(rows) > limit:
        print(f"... ({len(rows) - limit} filas más)")


def write_csv_out(out_path: Path, cols: Sequence[str], rows: Sequence[Tuple[Any, ...]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(list(cols))
        for r in rows:
            w.writerow(list(r))


def _fix_smart_quotes(s: str) -> str:
    return (
        s.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )


def parse_params_any(s: str) -> List[Any]:
    """
    Acepta:
      - JSON válido: ["bookpeople"], ["978..","bookpeople"], [978..,"bookpeople"]
      - Formatos relajados (PowerShell): [bookpeople], [978..,bookpeople], bookpeople, 978..,bookpeople
    """
    s = _fix_smart_quotes((s or "").strip())
    if not s:
        return []

    # 1) intentar JSON directo
    try:
        obj = json.loads(s)
        if isinstance(obj, list):
            return obj
        return [obj]
    except Exception:
        pass

    # 2) fallback: lista "relajada"
    t = s.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()

    if not t:
        return []

    parts = [p.strip() for p in t.split(",") if p.strip()]
    out: List[Any] = []

    for p in parts:
        # quitar comillas si las hay
        if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
            p = p[1:-1].strip()

        # ISBN: conservar como string (10/13 dígitos)
        if re.fullmatch(r"\d{10}|\d{13}", p):
            out.append(p)
            continue

        # int / float
        if re.fullmatch(r"-?\d+", p):
            try:
                out.append(int(p))
            except Exception:
                out.append(p)
            continue

        if re.fullmatch(r"-?\d+\.\d+", p):
            try:
                out.append(float(p))
            except Exception:
                out.append(p)
            continue

        out.append(p)

    return out


def _load_params_file(path: Path) -> List[Any]:
    if not path.exists():
        raise ValueError(f"params-file no existe: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("params-file debe contener una LISTA JSON. Ej: [\"bookpeople\"]")
    return data


def _coerce_param_scalar(p: str) -> Any:
    """
    Convierte un solo --param a valor:
      - 10/13 dígitos => string (ISBN)
      - int/float => número
      - resto => string
    """
    if p is None:
        return ""
    s = _fix_smart_quotes(str(p).strip())
    if not s:
        return ""

    # Si viene con comillas
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()

    if re.fullmatch(r"\d{10}|\d{13}", s):
        return s
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return s
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description="Ejecutor simple de SQL para BookSearchV2")
    ap.add_argument("--db", required=True, help="Ruta a la DB sqlite")
    ap.add_argument("--sql", required=True, help="Ruta al archivo .sql")

    # Parámetros (tres formas)
    ap.add_argument("--param", action="append", default=[], help="Parámetro individual (se puede repetir)")
    ap.add_argument("--params", default="", help="Parámetros (JSON o lista relajada)")
    ap.add_argument("--params-file", default="", help="Archivo .json con lista de params")

    ap.add_argument("--limit", type=int, default=200, help="Límite de filas a imprimir (0 = sin límite)")
    ap.add_argument("--out", default="", help="Opcional: exportar resultado a CSV")

    args = ap.parse_args()

    db_path = Path(args.db)
    sql_path = Path(args.sql)

    if not db_path.exists():
        raise SystemExit(f"[ERROR] DB no existe: {db_path}")
    if not sql_path.exists():
        raise SystemExit(f"[ERROR] SQL no existe: {sql_path}")

    # Resolver params (prioridad: --param > --params-file > --params)
    params: Optional[List[Any]] = None

    if args.param:
        params = [_coerce_param_scalar(p) for p in args.param]
    elif args.params_file.strip():
        try:
            params = _load_params_file(Path(args.params_file))
        except Exception as e:
            raise SystemExit(f"[ERROR] --params-file inválido: {e}")
    elif args.params.strip():
        try:
            params = parse_params_any(args.params)
        except Exception as e:
            raise SystemExit(f"[ERROR] --params inválido: {e}")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    sql_text = read_sql(sql_path)
    stmts = split_statements(sql_text)

    last_cols: List[str] = []
    last_rows: List[Tuple[Any, ...]] = []

    for stmt in stmts:
        if is_result_query(stmt):
            try:
                c = cur.execute(stmt, params) if params is not None else cur.execute(stmt)
            except sqlite3.ProgrammingError as e:
                # típicamente: "Incorrect number of bindings supplied"
                raise SystemExit(f"[ERROR] SQL bindings: {e}")
            rows = c.fetchall()
            cols = [d[0] for d in (c.description or [])]
            last_cols = cols
            last_rows = rows
            print(f"\n--- RESULT ({len(rows)} filas) ---")
            print_rows(cols, rows, limit=args.limit)
        else:
            cur.execute(stmt)
            con.commit()
            print("\n--- OK (statement ejecutado) ---")

    con.close()

    if args.out and last_cols:
        out_path = Path(args.out)
        write_csv_out(out_path, last_cols, last_rows)
        print(f"\n[OK] CSV escrito: {out_path}")

if __name__ == "__main__":
    main()