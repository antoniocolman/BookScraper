# tools/amazon_ingest_server.py
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
import uuid
import hashlib
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.storage.book_std_db import Database


def normalize_isbn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", (s or "")).upper().strip()


def isbn13_to_isbn10(isbn13: str) -> str:
    s = normalize_isbn(isbn13)
    if len(s) != 13 or not s.startswith("978"):
        return ""
    core9 = s[3:12]
    if len(core9) != 9 or not core9.isdigit():
        return ""
    total = 0
    for i, ch in enumerate(core9, start=1):
        total += i * int(ch)
    r = total % 11
    check = "X" if r == 10 else str(r)
    return core9 + check



def isbn10_to_isbn13(isbn10: str) -> str:
    """Convierte ISBN-10 a ISBN-13 (solo para prefijo 978)."""
    s = normalize_isbn(isbn10)
    if len(s) != 10:
        return ""
    base9 = s[:9]
    core = "978" + base9
    # check digit ISBN-13
    total = 0
    for i, ch in enumerate(core):
        d = ord(ch) - 48
        total += d * (1 if (i % 2 == 0) else 3)
    check = (10 - (total % 10)) % 10
    return core + str(check)


def _read_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        obj = json.loads(raw.decode("utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _now() -> int:
    return int(time.time())


def _is_loopback_ip(ip: str) -> bool:
    ip = (ip or "").strip()
    return ip in ("127.0.0.1", "::1")


def _load_users(users_file: str) -> Dict[str, str]:
    if not users_file:
        return {}
    p = Path(users_file)
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore")) or {}
        if not isinstance(obj, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in obj.items():
            kk = str(k).strip()
            vv = str(v).strip()
            if kk and vv:
                out[kk] = vv
        return out
    except Exception:
        return {}


@dataclass
class QueueItem:
    isbn: str
    site: str
    url: str
    titulo: str
    amazon_search_url: str


def load_queue_csv(queue_file: Path) -> List[QueueItem]:
    if not queue_file.exists():
        return []
    items: List[QueueItem] = []
    with queue_file.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            isbn = normalize_isbn(row.get("isbn", ""))
            if not isbn:
                continue
            items.append(
                QueueItem(
                    isbn=isbn,
                    site=(row.get("site") or "").strip(),
                    url=(row.get("url") or "").strip(),
                    titulo=(row.get("titulo") or "").strip(),
                    amazon_search_url=(row.get("amazon_search_url") or "").strip()
                    or f"https://www.amazon.com/s?k={isbn}&i=stripbooks",
                )
            )
    return items


def load_state(state_file: Path) -> Dict[str, Any]:
    if not state_file.exists():
        return {"queue_id": "", "done": [], "skipped": [], "in_progress": {}}
    try:
        obj = json.loads(state_file.read_text(encoding="utf-8", errors="ignore")) or {}
        if "queue_id" not in obj:
            obj["queue_id"] = ""
        if "done" not in obj:
            obj["done"] = []
        if "skipped" not in obj:
            obj["skipped"] = []
        if "in_progress" not in obj or not isinstance(obj.get("in_progress"), dict):
            obj["in_progress"] = {}
        return obj
    except Exception:
        return {"queue_id": "", "done": [], "skipped": [], "in_progress": {}}


def save_state(state_file: Path, state: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")



def _queue_id(queue: List[QueueItem]) -> str:
    """ID estable de la cola actual (cambia si cambia el contenido u orden)."""
    joined = "\n".join([it.isbn for it in queue])
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _sync_state_with_queue(queue: List[QueueItem], state: Dict[str, Any]) -> bool:
    """Limpia state viejo para que stats no se rompan al cambiar de cola.

    - recorta done/skipped/in_progress a los ISBN presentes en la cola actual
    - actualiza queue_id
    Retorna True si hubo cambios.
    """
    qset = {it.isbn for it in queue}
    changed = False

    # done / skipped: filtrar
    done = [normalize_isbn(x) for x in state.get("done", []) if normalize_isbn(x)]
    done_f = [x for x in done if x in qset]
    if done_f != done:
        changed = True
    skipped = [normalize_isbn(x) for x in state.get("skipped", []) if normalize_isbn(x)]
    skipped_f = [x for x in skipped if x in qset]
    if skipped_f != skipped:
        changed = True

    ip = state.get("in_progress") or {}
    ip_f = {}
    for isbn, info in ip.items():
        ni = normalize_isbn(isbn)
        if ni and ni in qset:
            ip_f[ni] = info
        else:
            changed = True

    state["done"] = done_f
    state["skipped"] = skipped_f
    state["in_progress"] = ip_f

    qid = _queue_id(queue)
    if state.get("queue_id") != qid:
        state["queue_id"] = qid
        changed = True

    return changed


def _cleanup_expired_leases(state: Dict[str, Any]) -> int:
    now = _now()
    ip = state.get("in_progress") or {}
    to_del = []
    for isbn, info in ip.items():
        try:
            if int(info.get("expires_at", 0)) <= now:
                to_del.append(isbn)
        except Exception:
            to_del.append(isbn)
    for isbn in to_del:
        ip.pop(isbn, None)
    state["in_progress"] = ip
    return len(to_del)


def _state_mark_list(state: Dict[str, Any], key: str, isbn: str) -> None:
    isbn = normalize_isbn(isbn)
    if not isbn:
        return
    arr = state.get(key) or []
    arr = [normalize_isbn(x) for x in arr if x]
    if isbn not in arr:
        arr.append(isbn)
    state[key] = arr


def _find_next_unclaimed(queue: List[QueueItem], state: Dict[str, Any]) -> Optional[QueueItem]:
    done = set(normalize_isbn(x) for x in state.get("done", []) if x)
    skipped = set(normalize_isbn(x) for x in state.get("skipped", []) if x)
    in_progress = set(normalize_isbn(x) for x in (state.get("in_progress") or {}).keys() if x)
    for it in queue:
        if it.isbn in done or it.isbn in skipped or it.isbn in in_progress:
            continue
        return it
    return None


def _compute_stats(queue: List[QueueItem], state: Dict[str, Any]) -> Dict[str, int]:
    qset = {it.isbn for it in queue}
    done_cnt = len({normalize_isbn(x) for x in state.get('done', []) if normalize_isbn(x) in qset})
    skipped_cnt = len({normalize_isbn(x) for x in state.get('skipped', []) if normalize_isbn(x) in qset})
    ip = state.get('in_progress') or {}
    inprog_cnt = len({normalize_isbn(x) for x in ip.keys() if normalize_isbn(x) in qset})
    total = len(queue)
    remaining = max(0, total - done_cnt - skipped_cnt - inprog_cnt)
    return {'total': total, 'done': done_cnt, 'skipped': skipped_cnt, 'in_progress': inprog_cnt, 'remaining': remaining}


def claim_next(queue_file: Path, state_file: Path, worker: str, ttl_seconds: int) -> Dict[str, Any]:
    queue = load_queue_csv(queue_file)
    state = load_state(state_file)
    _cleanup_expired_leases(state)
    changed = _sync_state_with_queue(queue, state)
    if changed:
        save_state(state_file, state)

    it = _find_next_unclaimed(queue, state)
    if not it:
        save_state(state_file, state)
        return {"ok": True, "done": True, "item": None, "lease_id": None, "stats": _compute_stats(queue, state)}

    lease_id = uuid.uuid4().hex
    now = _now()
    expires_at = now + max(30, int(ttl_seconds))

    ip = state.get("in_progress") or {}
    ip[it.isbn] = {"lease_id": lease_id, "worker": worker or "anon", "claimed_at": now, "expires_at": expires_at}
    state["in_progress"] = ip
    save_state(state_file, state)

    return {
        "ok": True,
        "done": False,
        "lease_id": lease_id,
        "lease_expires_at": expires_at,
        "item": {
            "isbn": it.isbn,
            "site": it.site,
            "url": it.url,
            "titulo": it.titulo,
            "amazon_search_url": it.amazon_search_url,
        },
        "stats": _compute_stats(queue, state),
    }


def resolve_lease(state: Dict[str, Any], lease_id: str) -> Optional[str]:
    lease_id = (lease_id or "").strip()
    if not lease_id:
        return None
    ip = state.get("in_progress") or {}
    for isbn, info in ip.items():
        if str(info.get("lease_id", "")) == lease_id:
            return isbn
    return None


def finish_lease(state_file: Path, lease_id: str, action: str) -> Dict[str, Any]:
    state = load_state(state_file)
    _cleanup_expired_leases(state)

    isbn = resolve_lease(state, lease_id)
    if not isbn:
        return {"ok": False, "error": "lease_not_found_or_expired"}

    state.get("in_progress", {}).pop(isbn, None)
    _state_mark_list(state, "done" if action == "done" else "skipped", isbn)
    save_state(state_file, state)
    return {"ok": True, "isbn": isbn, "action": action}


def release_lease(state_file: Path, lease_id: str) -> Dict[str, Any]:
    state = load_state(state_file)
    _cleanup_expired_leases(state)
    isbn = resolve_lease(state, lease_id)
    if not isbn:
        return {"ok": False, "error": "lease_not_found_or_expired"}
    state.get("in_progress", {}).pop(isbn, None)
    save_state(state_file, state)
    return {"ok": True, "isbn": isbn, "released": True}


def heartbeat_lease(state_file: Path, lease_id: str, ttl_seconds: int) -> Dict[str, Any]:
    state = load_state(state_file)
    _cleanup_expired_leases(state)
    isbn = resolve_lease(state, lease_id)
    if not isbn:
        return {"ok": False, "error": "lease_not_found_or_expired"}
    now = _now()
    new_exp = now + max(30, int(ttl_seconds))
    state["in_progress"][isbn]["expires_at"] = new_exp
    save_state(state_file, state)
    return {"ok": True, "isbn": isbn, "lease_id": lease_id, "lease_expires_at": new_exp}


def enrich_by_isbns_sqlite(db_path: str, isbns: List[str], src: Dict[str, Any]) -> int:
    isbns_n = [normalize_isbn(x) for x in isbns if normalize_isbn(x)]
    if not isbns_n:
        return 0

    def g(k: str) -> str:
        return str(src.get(k) or "").strip()

    payload = {
        "isbn": normalize_isbn(g("ISBN")) or normalize_isbn(g("ISBN10")),
        "titulo": g("TITULO"),
        "autor": g("AUTOR"),
        "editorial": g("EDITORIAL"),
        "sinopsis": g("SINOPSIS"),
        "idioma": g("IDIOMA"),
        "paginas": g("PAGINAS"),
        "dimensiones": g("DIMENSIONES"),
        "fecha_publicacion": g("FECHA PUBLICACION"),
        "url_portada": g("URL PORTADA"),
    }

    where_in = ",".join(["?"] * len(isbns_n))

    def norm_sql(col: str) -> str:
        return f"REPLACE(REPLACE({col},'-',''),' ', '')"

    sql = f"""
    UPDATE book_std
    SET
      isbn = CASE WHEN trim(ifnull(isbn,''))='' AND trim(?)<>'' THEN ? ELSE isbn END,
      titulo = CASE WHEN trim(ifnull(titulo,''))='' AND trim(?)<>'' THEN ? ELSE titulo END,
      autor = CASE WHEN trim(ifnull(autor,''))='' AND trim(?)<>'' THEN ? ELSE autor END,
      editorial = CASE WHEN trim(ifnull(editorial,''))='' AND trim(?)<>'' THEN ? ELSE editorial END,
      sinopsis = CASE WHEN trim(ifnull(sinopsis,''))='' AND trim(?)<>'' THEN ? ELSE sinopsis END,
      idioma = CASE WHEN trim(ifnull(idioma,''))='' AND trim(?)<>'' THEN ? ELSE idioma END,
      paginas = CASE WHEN trim(ifnull(paginas,''))='' AND trim(?)<>'' THEN ? ELSE paginas END,
      dimensiones = CASE WHEN trim(ifnull(dimensiones,''))='' AND trim(?)<>'' THEN ? ELSE dimensiones END,
      fecha_publicacion = CASE WHEN trim(ifnull(fecha_publicacion,''))='' AND trim(?)<>'' THEN ? ELSE fecha_publicacion END,
      url_portada = CASE WHEN trim(ifnull(url_portada,''))='' AND trim(?)<>'' THEN ? ELSE url_portada END,
      updated_at = datetime('now')
    WHERE {norm_sql('isbn')} IN ({where_in})
      AND site <> 'amazon_books'
    """

    params: List[Any] = []
    for key in [
        "isbn", "titulo", "autor", "editorial", "sinopsis",
        "idioma", "paginas", "dimensiones", "fecha_publicacion", "url_portada"
    ]:
        params.extend([payload[key], payload[key]])
    params.extend(isbns_n)

    con = sqlite3.connect(db_path)
    try:
        before = con.total_changes
        con.execute(sql, params)
        con.commit()
        after = con.total_changes
        return max(0, after - before)
    finally:
        con.close()



def db_lookup_sqlite(db_path: str, isbn: str, include_raw: bool = False) -> Dict[str, Any]:
    """Busca en book_std por ISBN (y su equivalente 10/13 si aplica).

    Devuelve:
      - found: bool
      - rows: lista de filas (sin RAW_JSON salvo include_raw)
      - best: fila elegida para preview
      - missing_fields: campos faltantes en best
      - raw_json_present / raw_json_size
    """
    q = normalize_isbn(isbn)
    if not q:
        return {"ok": True, "found": False, "isbn_query": isbn, "candidates": [], "rows": []}

    cands: List[str] = []
    def add(x: str):
        x = normalize_isbn(x)
        if x and x not in cands:
            cands.append(x)

    add(q)
    if len(q) == 13:
        add(isbn13_to_isbn10(q))
    elif len(q) == 10:
        add(isbn10_to_isbn13(q))

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join(["?"] * len(cands))
        qsql = f"""
        SELECT
            isbn AS "ISBN",
            titulo AS "TITULO",
            autor AS "AUTOR",
            editorial AS "EDITORIAL",
            encuadernacion AS "ENCUADERNACION",
            categoria AS "CATEGORIA",
            sinopsis AS "SINOPSIS",
            idioma AS "IDIOMA",
            paginas AS "PAGINAS",
            dimensiones AS "DIMENSIONES",
            fecha_publicacion AS "FECHA PUBLICACION",
            url AS "URL",
            url_portada AS "URL PORTADA",
            site AS "SITE",
            raw_json AS "RAW_JSON",
            updated_at AS "UPDATED_AT"
        FROM book_std
        WHERE isbn IN ({placeholders})
        """
        rows = [dict(r) for r in con.execute(qsql, cands).fetchall()]
    finally:
        con.close()

    if not rows:
        return {"ok": True, "found": False, "isbn_query": q, "candidates": cands, "rows": []}

    # elegir la mejor fila para preview: más campos completos
    prefer_site = ["amazon_books", "amazon", "amazon.com", "amazon_books_experimental"]

    score_fields = [
        "TITULO", "AUTOR", "EDITORIAL", "IDIOMA", "PAGINAS",
        "ENCUADERNACION", "FECHA PUBLICACION", "URL PORTADA", "SINOPSIS"
    ]

    def filled_count(r: Dict[str, Any]) -> int:
        n = 0
        for k in score_fields:
            v = str(r.get(k) or "").strip()
            if v:
                n += 1
        return n

    def site_rank(r: Dict[str, Any]) -> int:
        s = str(r.get("SITE") or "").strip().lower()
        for i, p in enumerate(prefer_site):
            if s == p:
                return i
        return len(prefer_site) + 1

    def updated_int(r: Dict[str, Any]) -> int:
        v = r.get("UPDATED_AT")
        try:
            return int(v)
        except Exception:
            return 0

    rows_sorted = sorted(rows, key=lambda r: (-filled_count(r), site_rank(r), -updated_int(r)))
    best = rows_sorted[0]

    raw = str(best.get("RAW_JSON") or "")
    raw_present = bool(raw.strip())
    raw_size = len(raw.encode("utf-8")) if raw_present else 0

    # missing fields en best
    missing = []
    for k in score_fields:
        if not str(best.get(k) or "").strip():
            missing.append(k)

    # por default no devolver RAW_JSON completo
    if not include_raw:
        for r in rows:
            r.pop("RAW_JSON", None)
        best_out = dict(best)
        best_out.pop("RAW_JSON", None)
    else:
        best_out = dict(best)

    return {
        "ok": True,
        "found": True,
        "isbn_query": q,
        "candidates": cands,
        "rows": rows,
        "best": best_out,
        "missing_fields": missing,
        "raw_json_present": raw_present,
        "raw_json_size": raw_size,
    }


def _queue_from_text_auto(text: str) -> List[QueueItem]:
    """
    Acepta:
      - CSV con header (isbn,...)
      - TXT (1 isbn por linea)
    Devuelve lista normalizada.
    """
    t = (text or "").strip()
    if not t:
        return []

    # heurística: si primera linea tiene 'isbn' y comas => CSV
    first = t.splitlines()[0].lower()
    is_csv = ("isbn" in first and "," in first)

    items: List[QueueItem] = []
    seen = set()

    if is_csv:
        f = StringIO(t)
        r = csv.DictReader(f)
        for row in r:
            isbn = normalize_isbn(row.get("isbn", ""))
            if not isbn or isbn in seen:
                continue
            seen.add(isbn)
            items.append(
                QueueItem(
                    isbn=isbn,
                    site=(row.get("site") or "").strip(),
                    url=(row.get("url") or "").strip(),
                    titulo=(row.get("titulo") or "").strip(),
                    amazon_search_url=(row.get("amazon_search_url") or "").strip()
                    or f"https://www.amazon.com/s?k={isbn}&i=stripbooks",
                )
            )
        return items

    # TXT
    for line in t.splitlines():
        isbn = normalize_isbn(line)
        if not isbn or isbn in seen:
            continue
        seen.add(isbn)
        items.append(
            QueueItem(
                isbn=isbn,
                site="",
                url="",
                titulo="",
                amazon_search_url=f"https://www.amazon.com/s?k={isbn}&i=stripbooks",
            )
        )
    return items


def _write_queue_csv(queue_file: Path, items: List[QueueItem]) -> None:
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    with queue_file.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["isbn", "site", "url", "titulo", "amazon_search_url"])
        for it in items:
            w.writerow([it.isbn, it.site, it.url, it.titulo, it.amazon_search_url])


class Handler(BaseHTTPRequestHandler):
    server_version = "AmazonIngestFIFO/0.4"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-User")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _send_json(self, status: int, obj: Dict[str, Any]):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self) -> str:
        try:
            return str(self.client_address[0])
        except Exception:
            return ""

    def _net_ok(self) -> Tuple[bool, str]:
        mode = getattr(self.server, "mode", "local") or "local"
        ip = self._client_ip()
        if mode == "local" and not _is_loopback_ip(ip):
            return False, "forbidden_local_mode"
        return True, ""

    def _auth_ok(self) -> Tuple[bool, str, str]:
        users: Dict[str, str] = getattr(self.server, "users", {}) or {}
        api_key = getattr(self.server, "api_key", "") or ""
        user = (self.headers.get("X-User", "") or "").strip()

        if users:
            key = (self.headers.get("X-API-Key", "") or "").strip()
            if not user:
                return False, "missing_user", ""
            if not key:
                return False, "missing_key", user
            if users.get(user) != key:
                return False, "unauthorized", user
            return True, "", user

        if api_key:
            key = (self.headers.get("X-API-Key", "") or "").strip()
            if key != api_key:
                return False, "unauthorized", user
            return True, "", user

        return True, "", user

    def _is_admin(self, authed_user: str) -> bool:
        admin_user = getattr(self.server, "admin_user", "admin") or "admin"
        return (authed_user or "") == admin_user

    def do_GET(self):
        ok_net, net_err = self._net_ok()
        if not ok_net:
            return self._send_json(403, {"ok": False, "error": net_err})

        ok_auth, auth_err, authed_user = self._auth_ok()
        if not ok_auth:
            return self._send_json(401, {"ok": False, "error": auth_err})

        if self.path.startswith("/health"):
            return self._send_json(
                200,
                {
                    "ok": True,
                    "mode": getattr(self.server, "mode", "local"),
                    "db_path": getattr(self.server, "db_path", ""),
                    "queue_file": str(getattr(self.server, "queue_file", "")),
                },
            )

        if self.path.startswith("/admin/mode"):
            return self._send_json(200, {"ok": True, "mode": getattr(self.server, "mode", "local")})

        if self.path.startswith("/db/lookup"):
            db_path = getattr(self.server, "db_path", r".\data\booksearchv2.db")
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query or "")
            isbn = normalize_isbn((qs.get("isbn", [""])[0] or ""))
            include_raw = (qs.get("include_raw", ["0"])[0] or "0") in ("1", "true", "yes")
            if not isbn:
                return self._send_json(400, {"ok": False, "error": "missing_isbn"})
            out = db_lookup_sqlite(db_path, isbn=isbn, include_raw=include_raw)
            return self._send_json(200, out)

        if self.path.startswith("/queue/next"):
            queue_file: Path = getattr(self.server, "queue_file", Path(r".\data\exports\enrich_queue_amazon.csv"))
            state_file: Path = getattr(self.server, "state_file", Path(r".\data\state\enrich_queue_state.json"))
            ttl = int(getattr(self.server, "lease_ttl", 900))
            out = claim_next(queue_file, state_file, worker="legacy", ttl_seconds=ttl)
            return self._send_json(200, out)

        if self.path.startswith("/queue/stats"):
            queue_file: Path = getattr(self.server, "queue_file", Path(r".\data\exports\enrich_queue_amazon.csv"))
            state_file: Path = getattr(self.server, "state_file", Path(r".\data\state\enrich_queue_state.json"))
            queue = load_queue_csv(queue_file)
            state = load_state(state_file)
            _cleanup_expired_leases(state)
            changed = _sync_state_with_queue(queue, state)
            if changed:
                save_state(state_file, state)
            else:
                # igual persistimos si limpiamos leases expirados
                save_state(state_file, state)
            return self._send_json(200, {"ok": True, "stats": _compute_stats(queue, state)})

        return self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        ok_net, net_err = self._net_ok()
        if not ok_net:
            return self._send_json(403, {"ok": False, "error": net_err})

        ok_auth, auth_err, authed_user = self._auth_ok()
        if not ok_auth:
            return self._send_json(401, {"ok": False, "error": auth_err})

        payload = _read_json(self)

        queue_file: Path = getattr(self.server, "queue_file", Path(r".\data\exports\enrich_queue_amazon.csv"))
        state_file: Path = getattr(self.server, "state_file", Path(r".\data\state\enrich_queue_state.json"))
        ttl = int(getattr(self.server, "lease_ttl", 900))

        # admin: cambiar modo
        if self.path == "/admin/mode":
            if not self._is_admin(authed_user):
                return self._send_json(403, {"ok": False, "error": "admin_required"})
            mode = str(payload.get("mode") or "").strip().lower()
            if mode not in ("local", "lan"):
                return self._send_json(400, {"ok": False, "error": "invalid_mode", "hint": "mode=local|lan"})
            self.server.mode = mode
            return self._send_json(200, {"ok": True, "mode": mode})

        # admin: reset state
        if self.path == "/admin/reset_state":
            if not self._is_admin(authed_user):
                return self._send_json(403, {"ok": False, "error": "admin_required"})
            if not bool(payload.get("confirm")):
                return self._send_json(400, {"ok": False, "error": "confirm_required"})
            save_state(state_file, {"done": [], "skipped": [], "in_progress": {}})
            return self._send_json(200, {"ok": True, "reset": True})

        # ✅ admin: subir cola sin apagar server
        if self.path in ("/admin/upload_queue", "/admin/queue/load"):
            if not self._is_admin(authed_user):
                return self._send_json(403, {"ok": False, "error": "admin_required"})
            text = str(payload.get("text") or "")
            if not text.strip():
                return self._send_json(400, {"ok": False, "error": "missing_text"})
            items = _queue_from_text_auto(text)
            _write_queue_csv(queue_file, items)
            if bool(payload.get("reset_state", True)):
                save_state(state_file, {"done": [], "skipped": [], "in_progress": {}})
            return self._send_json(200, {"ok": True, "items": len(items), "queue_file": str(queue_file)})

        if self.path == "/queue/claim":
            worker = (payload.get("worker") or authed_user or "anon").strip()
            ttl_seconds = int(payload.get("ttl_seconds") or ttl)
            out = claim_next(queue_file, state_file, worker=worker, ttl_seconds=ttl_seconds)
            return self._send_json(200, out)

        if self.path == "/queue/heartbeat":
            lease_id = (payload.get("lease_id") or "").strip()
            ttl_seconds = int(payload.get("ttl_seconds") or ttl)
            out = heartbeat_lease(state_file, lease_id=lease_id, ttl_seconds=ttl_seconds)
            return self._send_json(200 if out.get("ok") else 409, out)

        if self.path == "/queue/release":
            lease_id = (payload.get("lease_id") or "").strip()
            out = release_lease(state_file, lease_id=lease_id)
            return self._send_json(200 if out.get("ok") else 409, out)

        if self.path in ("/queue/done", "/queue/skip"):
            lease_id = (payload.get("lease_id") or "").strip()
            out = finish_lease(state_file, lease_id=lease_id, action="done" if self.path.endswith("/done") else "skipped")
            return self._send_json(200 if out.get("ok") else 409, out)

        # ingest/enrich
        row = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(row, dict):
            row = {}

        site = (row.get("SITE") or row.get("site") or "").strip()
        if site.lower() == "amazon":
            row["SITE"] = "amazon_books"

        db_path = getattr(self.server, "db_path", r".\data\booksearchv2.db")

        if self.path == "/ingest":
            if not (row.get("SITE") and row.get("URL")):
                return self._send_json(400, {"ok": False, "error": "missing_SITE_or_URL"})
            db = Database(db_path)
            try:
                ins, upd = db.upsert_books([row])
            finally:
                db.close()
            return self._send_json(200, {"ok": True, "inserted": ins, "updated": upd})

        if self.path == "/enrich":
            target_isbn = normalize_isbn(payload.get("target_isbn", ""))

            ins, upd = (0, 0)
            if row.get("SITE") and row.get("URL"):
                db = Database(db_path)
                try:
                    ins, upd = db.upsert_books([row])
                finally:
                    db.close()

            isbn13 = normalize_isbn(row.get("ISBN", ""))
            isbn10 = normalize_isbn(row.get("ISBN10", "")) or isbn13_to_isbn10(isbn13)
            candidates: List[str] = []
            if target_isbn:
                candidates.append(target_isbn)
            for c in [isbn13, isbn10, isbn13_to_isbn10(isbn13)]:
                c = normalize_isbn(c)
                if c and c not in candidates:
                    candidates.append(c)

            enriched = enrich_by_isbns_sqlite(db_path, candidates, row)
            return self._send_json(
                200,
                {"ok": True, "inserted_amazon": ins, "updated_amazon": upd, "enriched_rows": enriched, "isbns": candidates},
            )

        return self._send_json(404, {"ok": False, "error": "not_found"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db-path", default=r".\data\booksearchv2.db")
    ap.add_argument("--queue-file", default=r".\data\exports\enrich_queue_amazon.csv")
    ap.add_argument("--state-file", default=r".\data\state\enrich_queue_state.json")
    ap.add_argument("--mode", default="local", choices=["local", "lan"])
    ap.add_argument("--users-file", default="", help="JSON: {'admin':'KEY','PC-A':'KEY2'}")
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--lease-ttl", type=int, default=900)
    args = ap.parse_args()

    httpd = HTTPServer((args.host, args.port), Handler)
    httpd.db_path = args.db_path
    httpd.queue_file = Path(args.queue_file)
    httpd.state_file = Path(args.state_file)
    httpd.lease_ttl = int(args.lease_ttl)
    httpd.mode = args.mode
    httpd.admin_user = args.admin_user
    httpd.users = _load_users(args.users_file)
    httpd.api_key = args.api_key

    print(f"[OK] Server: http://{args.host}:{args.port}")
    print(f"[OK] Mode: {httpd.mode}")
    print(f"[OK] DB: {args.db_path}")
    print(f"[OK] Queue CSV: {args.queue_file}")
    print(f"[OK] State: {args.state_file}")
    print(f"[OK] Lease TTL: {httpd.lease_ttl}s")
    if httpd.users:
        print(f"[OK] Auth: users-file (users={len(httpd.users)}) admin={httpd.admin_user}")
    elif httpd.api_key:
        print("[OK] Auth: api-key global")
    else:
        print("[WARN] Auth: deshabilitado (solo recomendado en local)")
    print("[OK] Extra endpoints: POST /admin/upload_queue {text, reset_state?}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
