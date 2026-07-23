from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque

from .config import load_config, verify_access_code

SESSION_TTL_SECONDS = 12 * 60 * 60
ATTEMPT_WINDOW_SECONDS = 5 * 60
MAX_ATTEMPTS = 8
_attempts: dict[str, deque[float]] = defaultdict(deque)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _can_attempt(ip: str) -> bool:
    now = time.time()
    queue = _attempts[ip]
    while queue and queue[0] < now - ATTEMPT_WINDOW_SECONDS:
        queue.popleft()
    return len(queue) < MAX_ATTEMPTS


def authenticate(code: str, ip: str) -> str | None:
    if not _can_attempt(ip):
        return None
    config = load_config()
    if not verify_access_code(code, config["code_hash"]):
        _attempts[ip].append(time.time())
        return None
    payload = {
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": _b64(hashlib.sha256(f"{time.time_ns()}:{ip}".encode()).digest()[:16]),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    secret = _unb64(config["session_secret"])
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def verify_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        payload_text, signature_text = token.split(".", 1)
        raw = _unb64(payload_text)
        signature = _unb64(signature_text)
        secret = _unb64(load_config()["session_secret"])
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(raw)
        return int(payload["exp"]) >= int(time.time())
    except Exception:
        return False
