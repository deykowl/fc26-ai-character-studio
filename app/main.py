from __future__ import annotations

import html
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import authenticate, verify_session
from .config import PROJECTS_DIR, load_config
from .engine.fitter import RUNTIME
from .engine.jobs import create_fit_job, create_refine_job, get_job

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATE_DIR = APP_DIR / "templates"

app = FastAPI(title="FC26 AI Character Studio", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RefineRequest(BaseModel):
    axis_values: list[float] = Field(min_length=618, max_length=618)
    locked_regions: list[str] = []
    target_regions: list[str] | None = None


def _logged(request: Request) -> bool:
    return verify_session(request.cookies.get("fc26_session"))


def _require(request: Request) -> None:
    if not _logged(request):
        raise HTTPException(status_code=401, detail="Connexion requise")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@app.get("/")
async def index(request: Request):
    if not _logged(request):
        return RedirectResponse("/login")
    return FileResponse(TEMPLATE_DIR / "index.html")


@app.get("/login")
async def login_page(request: Request):
    if _logged(request):
        return RedirectResponse("/")
    return FileResponse(TEMPLATE_DIR / "login.html")


@app.post("/api/login")
async def login(request: Request, code: str = Form(...)):
    ip = request.client.host if request.client else "local"
    token = authenticate(code, ip)
    if not token:
        return JSONResponse({"ok": False, "error": "Code invalide ou trop de tentatives."}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "fc26_session",
        token,
        httponly=True,
        secure=False,
        samesite="strict",
        max_age=12 * 60 * 60,
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("fc26_session")
    return response


@app.get("/api/status")
async def status(request: Request):
    _require(request)
    return {
        "name": "FC26 AI Character Studio",
        "version": "1.0.0",
        "renderer": "exact-fc26-lod0",
        "vertices": int(RUNTIME.vertices.shape[0]),
        "triangles": int(RUNTIME.faces.shape[0]),
        "runtime_morphs": int(len(RUNTIME.offsets) - 1),
        "ui_axes": int(len(RUNTIME.axis_meta)),
        "ui_controls": int(len(RUNTIME.controls)),
        "image_processing": "local-machine",
    }




@app.get("/api/projects")
async def list_projects(request: Request):
    _require(request)
    projects = []
    for folder in PROJECTS_DIR.iterdir():
        if not folder.is_dir() or not (folder / "project.json").exists():
            continue
        try:
            project = json.loads((folder / "project.json").read_text(encoding="utf-8"))
            recipe_path = folder / "recipe.json"
            recipe = json.loads(recipe_path.read_text(encoding="utf-8")) if recipe_path.exists() else {}
            projects.append({
                "id": folder.name,
                "name": project.get("name", "Personnage"),
                "views": len(project.get("views", [])),
                "score": recipe.get("score"),
                "updated_at": recipe_path.stat().st_mtime if recipe_path.exists() else folder.stat().st_mtime,
            })
        except Exception:
            continue
    projects.sort(key=lambda item: item["updated_at"], reverse=True)
    return {"projects": projects[:30]}


@app.get("/api/projects/{project_id}")
async def read_project(request: Request, project_id: str):
    _require(request)
    folder = PROJECTS_DIR / project_id
    if not (folder / "project.json").exists() or not (folder / "recipe.json").exists():
        raise HTTPException(404, "Projet introuvable")
    return {
        "project": json.loads((folder / "project.json").read_text(encoding="utf-8")),
        "recipe": json.loads((folder / "recipe.json").read_text(encoding="utf-8")),
    }


@app.post("/api/projects")
async def create_project(
    request: Request,
    name: str = Form("Nouveau personnage"),
    images: list[UploadFile] = File(...),
):
    _require(request)
    if not 1 <= len(images) <= 8:
        raise HTTPException(400, "Ajoute entre 1 et 8 images.")
    max_bytes = int(load_config().get("max_upload_mb", 80)) * 1024 * 1024
    total = 0
    payloads: list[tuple[str, bytes]] = []
    allowed = {"image/jpeg", "image/png", "image/webp"}
    for upload in images:
        if upload.content_type not in allowed:
            raise HTTPException(400, f"Format non accepté : {upload.filename}")
        payload = await upload.read()
        total += len(payload)
        if total > max_bytes:
            raise HTTPException(413, "Ensemble d’images trop lourd.")
        payloads.append((upload.filename or "image", payload))
    project_id = uuid.uuid4().hex
    job_id = create_fit_job(project_id, name.strip() or "Nouveau personnage", payloads)
    return {"ok": True, "project_id": project_id, "job_id": job_id}


@app.post("/api/projects/{project_id}/refine")
async def refine_project(request: Request, project_id: str, body: RefineRequest):
    _require(request)
    folder = PROJECTS_DIR / project_id
    if not (folder / "views.json").exists():
        raise HTTPException(404, "Projet introuvable")
    allowed_regions = set(RUNTIME.region_indices)
    locked = [region for region in body.locked_regions if region in allowed_regions]
    target = None
    if body.target_regions:
        target = [region for region in body.target_regions if region in allowed_regions]
    job_id = create_refine_job(project_id, body.axis_values, locked, target)
    return {"ok": True, "project_id": project_id, "job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str):
    _require(request)
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "Calcul introuvable")
    return job


@app.get("/api/projects/{project_id}/recipe.json")
async def recipe_json(request: Request, project_id: str):
    _require(request)
    path = PROJECTS_DIR / project_id / "recipe.json"
    if not path.exists():
        raise HTTPException(404, "Recette indisponible")
    return FileResponse(path, media_type="application/json", filename="FC26_Character_Recipe.json")




@app.get("/api/projects/{project_id}/recipe.csv")
async def recipe_csv(request: Request, project_id: str):
    _require(request)
    folder = PROJECTS_DIR / project_id
    path = folder / "recipe.json"
    if not path.exists():
        raise HTTPException(404, "Recette indisponible")
    import csv
    from io import StringIO
    recipe = json.loads(path.read_text(encoding="utf-8"))
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Catégorie", "Couche", "Contrôle", "ID FC26", "Axe", "Valeur", "Pourcentage", "Morph EA"])
    for control in recipe["controls"]:
        for axis in control["axes"]:
            writer.writerow([
                control.get("category", "Head"), control["layer"],
                control.get("display_name", control["control"]), control["id"],
                axis["axis"], axis["value"], axis["percent"], axis["morph"],
            ])
    payload = "\ufeff" + output.getvalue()
    return HTMLResponse(
        payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="FC26_Character_Recipe.csv"'},
    )


AXIS_LABELS = {
    "D_U": "Bas ↔ Haut", "N_W": "Étroit ↔ Large", "B_F": "Arrière ↔ Avant",
    "R_A": "Rotation arrière ↔ avant", "SL_SR": "Rotation gauche ↔ droite",
    "L_M": "Fin ↔ massif", "L": "Relâché", "M": "Massif", "U": "Haut",
    "D": "Bas", "F": "Avant", "B": "Arrière", "R": "Rotation",
    "N": "Étroit", "W": "Large", "A": "Angle",
}


@app.get("/api/projects/{project_id}/recipe.html")
async def recipe_html(request: Request, project_id: str):
    _require(request)
    folder = PROJECTS_DIR / project_id
    recipe_path = folder / "recipe.json"
    if not recipe_path.exists():
        raise HTTPException(404, "Recette indisponible")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    project = json.loads((folder / "project.json").read_text(encoding="utf-8"))
    layers: dict[str, list[dict[str, Any]]] = {}
    for control in recipe["controls"]:
        if control["strength"] < 0.0005:
            continue
        layers.setdefault(control["layer"], []).append(control)
    sections = []
    for layer, controls in layers.items():
        controls.sort(key=lambda item: (item.get('category', ''), item.get('display_name', item['control'])))
        cards = []
        for control in controls:
            axis_rows = "".join(
                f"<tr><td>{html.escape(AXIS_LABELS.get(axis['axis'], axis['axis']))}</td>"
                f"<td>{axis['percent']:+.2f}%</td><td>{html.escape(axis['morph'])}</td></tr>"
                for axis in control["axes"] if abs(axis["value"]) >= 0.0005
            )
            cards.append(
                f"<article><h3>{html.escape(control.get('display_name', control['control']))}</h3>"
                f"<div class='technical'>{html.escape(control['id'])}</div>"
                f"<table><thead><tr><th>Axe</th><th>Valeur</th><th>Morph FC26</th></tr></thead>"
                f"<tbody>{axis_rows}</tbody></table></article>"
            )
        sections.append(f"<section><h2>{html.escape(layer)}</h2>{''.join(cards)}</section>")
    page = f"""<!doctype html><html lang='fr'><meta charset='utf-8'><title>Recette FC26 — {html.escape(project['name'])}</title>
<style>body{{font-family:Arial,sans-serif;background:#0c0d11;color:#f3f4f7;max-width:1100px;margin:auto;padding:34px}}h1{{font-size:38px}}h2{{color:#c8ff43;border-bottom:1px solid #333;padding-bottom:8px}}article{{background:#151820;border:1px solid #2a2e39;border-radius:12px;padding:16px;margin:12px 0}}h3{{margin:0 0 4px}}.technical{{color:#8f97a8;font:12px monospace;margin-bottom:12px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border-top:1px solid #292d36;text-align:left}}.score{{font-size:22px;color:#c8ff43}}.note{{background:#201f13;border:1px solid #5b5421;padding:13px;border-radius:9px}}</style>
<h1>{html.escape(project['name'])}</h1><p class='score'>Score géométrique : {recipe.get('score',0):.2f}%</p>
<p class='note'>Valeurs exactes du moteur Cranium normalisées entre −100% et +100%. Les noms techniques servent de référence stable aux données FC26.</p>
{''.join(sections)}</html>"""
    return HTMLResponse(page)
