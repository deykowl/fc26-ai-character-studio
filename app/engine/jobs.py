from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from ..config import PROJECTS_DIR
from .face_reader import read_faces
from .fitter import fit_views

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fc26-studio")
_LOCK = threading.RLock()
_JOBS: dict[str, dict[str, Any]] = {}


def _clean(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    return value


def _set(job_id: str, **updates: Any) -> None:
    with _LOCK:
        _JOBS[job_id].update(updates)
        _JOBS[job_id]["updated_at"] = time.time()


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return json.loads(json.dumps(job, default=_clean))


def _new_job(project_id: str, kind: str) -> str:
    job_id = uuid.uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "project_id": project_id,
            "kind": kind,
            "state": "queued",
            "progress": 1,
            "message": "Projet placé dans la file",
            "snapshots": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    return job_id


def create_fit_job(project_id: str, name: str, images: list[tuple[str, bytes]]) -> str:
    job_id = _new_job(project_id, "create")
    _EXECUTOR.submit(_run_create, job_id, project_id, name, images)
    return job_id


def create_refine_job(
    project_id: str,
    initial_theta: list[float],
    locked_regions: list[str],
    target_regions: list[str] | None,
) -> str:
    job_id = _new_job(project_id, "refine")
    _EXECUTOR.submit(
        _run_refine, job_id, project_id, initial_theta, locked_regions, target_regions
    )
    return job_id


def _snapshot_callback(job_id: str, snapshot: dict[str, Any]) -> None:
    stage_index = int(snapshot["stage_index"])
    iteration = int(snapshot["iteration"])
    progress = 25 + int(((stage_index * 4 + iteration) / 20) * 72)
    with _LOCK:
        history = _JOBS[job_id].setdefault("snapshots", [])
        history.append(snapshot)
        if len(history) > 120:
            del history[:-120]
    _set(
        job_id,
        state="optimizing",
        progress=min(progress, 97),
        message=f"{snapshot['stage']} — passe {iteration}/4",
        latest_snapshot=snapshot,
        score=snapshot["score"],
    )


def _project_dir(project_id: str) -> Path:
    path = PROJECTS_DIR / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_result(project_id: str, name: str, views: list[dict[str, Any]], result: dict[str, Any]) -> None:
    folder = _project_dir(project_id)
    public_views = [
        {key: value for key, value in view.items() if key != "landmarks"}
        for view in views
    ]
    (folder / "project.json").write_text(
        json.dumps({"id": project_id, "name": name, "views": public_views}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "views.json").write_text(
        json.dumps(views, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "recipe.json").write_text(
        json.dumps(result["recipe"], indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _run_create(job_id: str, project_id: str, name: str, images: list[tuple[str, bytes]]) -> None:
    try:
        _set(job_id, state="detecting", progress=5, message="Lecture locale des images")
        views = read_faces(images)
        _set(
            job_id,
            state="detected",
            progress=22,
            message=f"{len(views)} vue(s) faciale(s) détectée(s)",
            views=[{key: value for key, value in view.items() if key != "landmarks"} for view in views],
        )
        result = fit_views(views, progress=lambda snapshot: _snapshot_callback(job_id, snapshot))
        _write_result(project_id, name, views, result)
        _set(
            job_id,
            state="done",
            progress=100,
            message="Visage FC26 généré",
            result=result,
            score=result["recipe"]["score"],
        )
    except Exception as exc:
        _set(job_id, state="error", progress=100, message=str(exc), error=f"{type(exc).__name__}: {exc}")


def _run_refine(
    job_id: str,
    project_id: str,
    initial_theta: list[float],
    locked_regions: list[str],
    target_regions: list[str] | None,
) -> None:
    try:
        folder = _project_dir(project_id)
        views = json.loads((folder / "views.json").read_text(encoding="utf-8"))
        project = json.loads((folder / "project.json").read_text(encoding="utf-8"))
        _set(job_id, state="optimizing", progress=12, message="Nouvelle optimisation ciblée")
        result = fit_views(
            views,
            progress=lambda snapshot: _snapshot_callback(job_id, snapshot),
            initial_theta=initial_theta,
            locked_regions=set(locked_regions),
            target_regions=set(target_regions) if target_regions else None,
        )
        _write_result(project_id, project.get("name", "Personnage"), views, result)
        _set(
            job_id,
            state="done",
            progress=100,
            message="Affinage terminé",
            result=result,
            score=result["recipe"]["score"],
        )
    except Exception as exc:
        _set(job_id, state="error", progress=100, message=str(exc), error=f"{type(exc).__name__}: {exc}")
