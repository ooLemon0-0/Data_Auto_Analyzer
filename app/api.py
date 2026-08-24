from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.core.config import settings
from app.core.registry import build_source
from app.services.review_service import review_service

logger = logging.getLogger("data_review_platform")

app = FastAPI(title="Multi Project Data Review Platform", version="0.1.0")
db.init_db()

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class PrepareRequest(BaseModel):
    project_id: str
    business_date: date
    target_size: int = Field(ge=1)


class DecisionRequest(BaseModel):
    project_id: str
    business_date: date
    queue_id: int
    decision: str


class NavigateRequest(BaseModel):
    project_id: str
    business_date: date
    queue_id: int
    direction: str


class UploadRequest(BaseModel):
    project_id: str
    business_date: date


class SinkAuthRequest(BaseModel):
    project_id: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/projects")
def projects():
    return [
        {"id": p.id, "name": p.name, "daily_target": p.daily_target}
        for p in settings.projects
        if p.enabled
    ]


@app.get("/api/review/state")
def review_state(project_id: str, business_date: date):
    try:
        return review_service.state(project_id, business_date)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/review/prepare")
def prepare(req: PrepareRequest):
    try:
        return review_service.prepare(req.project_id, req.business_date, req.target_size)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/review/decision")
def decision(req: DecisionRequest):
    try:
        return review_service.decide(req.project_id, req.business_date, req.queue_id, req.decision)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/review/navigate")
def navigate(req: NavigateRequest):
    try:
        return review_service.navigate(req.project_id, req.business_date, req.queue_id, req.direction)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sink/auth")
def sink_auth(req: SinkAuthRequest):
    try:
        return review_service.prepare_sink_auth(req.project_id)
    except Exception as exc:
        logger.exception("Sink auth failed: project=%s", req.project_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/review/upload")
def upload(req: UploadRequest):
    try:
        return review_service.upload(req.project_id, req.business_date)
    except Exception as exc:
        logger.exception(
            "Review upload failed: project=%s date=%s",
            req.project_id,
            req.business_date,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/items/{item_id}/image")
def item_image(item_id: int):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT project_id, business_date, source_key, image_path, image_url FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")

    image_path = (row["image_path"] or "").strip()
    if image_path:
        path = Path(image_path).resolve()
        if path.exists():
            return FileResponse(path)

    image_url = (row["image_url"] or "").strip()
    if not image_url:
        raise HTTPException(status_code=404, detail="Image has neither local path nor source URL")

    suffix = Path(urlparse(image_url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}:
        suffix = ".jpg"
    destination = (
        Path(settings.storage.data_root).resolve()
        / row["project_id"]
        / row["business_date"]
        / "images"
        / f"{item_id}{suffix}"
    )

    try:
        project = settings.project(row["project_id"]).model_dump()
        source = build_source(project)
        path = source.materialize_image(image_url, destination)
        with db.connect() as conn:
            conn.execute("UPDATE items SET image_path=? WHERE id=?", (str(path.resolve()), item_id))
        return FileResponse(path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Image cache failed: {exc}") from exc