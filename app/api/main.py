from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.db.engine import get_engine
from app.db.repositories import book_std_repo
from app.standard import to_standard_row


def _get_api_key() -> str:
    return os.getenv("API_KEY", "").strip()


def _get_cors_origins() -> List[str]:
    raw = os.getenv("API_CORS_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [p.strip() for p in raw.split(",") if p.strip()]


def _auth(api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    required = _get_api_key()
    if not required:
        return
    if not api_key or api_key != required:
        raise HTTPException(status_code=401, detail="unauthorized")


class CapturePayload(BaseModel):
    data: Dict[str, Any] = Field(default_factory=dict)


class CaptureResponse(BaseModel):
    ok: bool
    inserted: int


app = FastAPI(title="BookSearchV2 API", version="0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _missing_fields(row: Dict[str, Any]) -> List[str]:
    fields = [
        "titulo",
        "autor",
        "editorial",
        "sinopsis",
        "idioma",
        "paginas",
        "dimensiones",
        "fecha_publicacion",
        "url_portada",
    ]
    missing = []
    for f in fields:
        v = row.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f)
    return missing


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "ts": datetime.utcnow().isoformat()}


@app.get("/isbn/{isbn}")
def get_by_isbn(isbn: str, _auth_ok: None = Depends(_auth)) -> Dict[str, Any]:
    engine = get_engine()
    with engine.connect() as conn:
        rows = book_std_repo.fetch_by_isbns(conn, [isbn])
    out = []
    for r in rows:
        rr = dict(r)
        rr["missing_fields"] = _missing_fields(rr)
        out.append(rr)
    return {"ok": True, "count": len(out), "rows": out}


@app.get("/missing")
def get_missing(limit: int = 50, _auth_ok: None = Depends(_auth)) -> Dict[str, Any]:
    engine = get_engine()
    with engine.connect() as conn:
        rows = book_std_repo.fetch_missing_fields(conn, limit=limit)
    out = []
    for r in rows:
        rr = dict(r)
        rr["missing_fields"] = _missing_fields(rr)
        out.append(rr)
    return {"ok": True, "count": len(out), "rows": out}


@app.post("/captures", response_model=CaptureResponse)
def post_capture(payload: CapturePayload, _auth_ok: None = Depends(_auth)) -> CaptureResponse:
    raw = payload.data or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")

    std = to_standard_row(raw, site_id=raw.get("SITE") or raw.get("site"))
    if not std.get("SITE") or not std.get("URL"):
        raise HTTPException(status_code=400, detail="missing_SITE_or_URL")

    now = datetime.utcnow().isoformat()
    row = {
        "site": std.get("SITE"),
        "url": std.get("URL"),
        "isbn": std.get("ISBN"),
        "titulo": std.get("TITULO"),
        "autor": std.get("AUTOR"),
        "editorial": std.get("EDITORIAL"),
        "encuadernacion": std.get("ENCUADERNACION"),
        "categoria": std.get("CATEGORIA"),
        "sinopsis": std.get("SINOPSIS"),
        "idioma": std.get("IDIOMA"),
        "paginas": std.get("PAGINAS"),
        "dimensiones": std.get("DIMENSIONES"),
        "fecha_publicacion": std.get("FECHA PUBLICACION"),
        "url_portada": std.get("URL PORTADA"),
        "raw_json": json.dumps({"raw": raw, "std": std}, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }

    engine = get_engine()
    with engine.begin() as conn:
        inserted, _updated = book_std_repo.upsert_many(conn, [row])

    return CaptureResponse(ok=True, inserted=inserted)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8080"))
    uvicorn.run("app.api.main:app", host=host, port=port, reload=False)
