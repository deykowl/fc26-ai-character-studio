from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
PROJECTS_DIR = WORKSPACE / "projects"
CONFIG_PATH = WORKSPACE / "config.json"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
PBKDF2_ITERATIONS = 350_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def hash_access_code(code: str, salt: bytes | None = None) -> dict[str, Any]:
    if len(code) < 6:
        raise ValueError("Le code d’accès doit contenir au moins 6 caractères.")
    salt = salt or secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {"salt": _b64(salt), "digest": _b64(digest), "iterations": PBKDF2_ITERATIONS}


def verify_access_code(code: str, record: dict[str, Any]) -> bool:
    salt = _unb64(record["salt"])
    digest = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, int(record.get("iterations", PBKDF2_ITERATIONS))
    )
    return hmac.compare_digest(digest, _unb64(record["digest"]))


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError("Le Studio n’est pas configuré. Lance setup_windows.bat.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any]) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def create_config(code: str) -> dict[str, Any]:
    config = {
        "code_hash": hash_access_code(code),
        "session_secret": _b64(secrets.token_bytes(48)),
        "created_at": int(time.time()),
        "host": "127.0.0.1",
        "port": 8765,
        "max_upload_mb": 80,
    }
    save_config(config)
    return config
