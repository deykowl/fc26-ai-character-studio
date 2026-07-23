from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:
    winreg = None

REPO_NAME = "deykowl/fc26-ai-character-studio"
MAX_SAMPLE_FILES = 250
HASH_LIMIT = 256 * 1024 * 1024
INTERESTING_EXTENSIONS = {
    ".exe", ".dll", ".toc", ".sb", ".cat", ".cas", ".bundle", ".bin",
    ".json", ".ini", ".cfg", ".dat", ".sav", ".save", ".profile", ".db",
    ".sqlite", ".lua", ".dds", ".png", ".fifamod", ".fbmod",
}


def sha256(path: Path) -> str | None:
    try:
        if path.stat().st_size > HASH_LIMIT:
            return None
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def safe_stat(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        return {
            "name": path.name,
            "size": stat.st_size,
            "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            "extension": path.suffix.lower(),
            "sha256": sha256(path),
        }
    except OSError:
        return None


def sanitize_path(path: Path) -> str:
    text = str(path.resolve())
    home = str(Path.home().resolve())
    if text.lower().startswith(home.lower()):
        text = "%USERPROFILE%" + text[len(home):]
    return text.replace("\\", "/")


def registry_install_candidates() -> list[Path]:
    if winreg is None:
        return []
    candidates: list[Path] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, key_path in roots:
        try:
            with winreg.OpenKey(hive, key_path) as root:
                count = winreg.QueryInfoKey(root)[0]
                for i in range(count):
                    try:
                        sub_name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, sub_name) as sub:
                            values = {}
                            for key in ("DisplayName", "InstallLocation", "DisplayIcon"):
                                try:
                                    values[key] = winreg.QueryValueEx(sub, key)[0]
                                except OSError:
                                    pass
                            display = str(values.get("DisplayName", ""))
                            if "FC 26" not in display.upper() and "EA SPORTS FC™ 26" not in display.upper():
                                continue
                            for value in (values.get("InstallLocation"), values.get("DisplayIcon")):
                                if not value:
                                    continue
                                cleaned = str(value).strip('"').split(",")[0]
                                p = Path(cleaned)
                                candidates.append(p if p.is_dir() else p.parent)
                    except OSError:
                        continue
        except OSError:
            continue
    return candidates


def common_install_candidates() -> list[Path]:
    paths: list[Path] = []
    names = [
        Path("Program Files/EA Games/EA SPORTS FC 26"),
        Path("Program Files/EA Games/EA SPORTS FC™ 26"),
        Path("Program Files (x86)/Steam/steamapps/common/EA SPORTS FC 26"),
        Path("Program Files/Epic Games/EA SPORTS FC 26"),
        Path("Games/EA SPORTS FC 26"),
        Path("EA Games/EA SPORTS FC 26"),
    ]
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:/")
        if not root.exists():
            continue
        for rel in names:
            p = root / rel
            if p.exists():
                paths.append(p)
    return paths


def normalize_install_root(path: Path) -> Path | None:
    if not path.exists():
        return None
    if path.is_file():
        path = path.parent
    direct = path / "FC26.exe"
    if direct.exists():
        return path
    # Bounded lookup: only a few levels, never crawl the whole disk.
    try:
        for exe in path.glob("**/FC26.exe"):
            return exe.parent
    except OSError:
        return None
    return None


def inspect_tree(root: Path) -> dict[str, Any]:
    extension_counts: dict[str, int] = {}
    extension_bytes: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0

    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in {
            "__pycache__", ".git", "screenshots", "crashdumps", "logs"
        }]
        current_path = Path(current)
        for filename in files:
            path = current_path / filename
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total_files += 1
            total_bytes += size
            ext = path.suffix.lower() or "<no_extension>"
            extension_counts[ext] = extension_counts.get(ext, 0) + 1
            extension_bytes[ext] = extension_bytes.get(ext, 0) + size

            if len(samples) < MAX_SAMPLE_FILES and (
                ext in INTERESTING_EXTENSIONS
                or filename.lower() in {"layout.toc", "initfs_win32", "package.mft", "version.json"}
            ):
                item = safe_stat(path)
                if item:
                    item["relative_path"] = path.relative_to(root).as_posix()
                    samples.append(item)

    top_extensions = sorted(
        (
            {"extension": ext, "count": count, "bytes": extension_bytes.get(ext, 0)}
            for ext, count in extension_counts.items()
        ),
        key=lambda x: (x["bytes"], x["count"]),
        reverse=True,
    )[:80]

    return {
        "root": sanitize_path(root),
        "total_files": total_files,
        "total_bytes": total_bytes,
        "top_extensions": top_extensions,
        "interesting_samples": samples,
    }


def save_candidates() -> list[Path]:
    home = Path.home()
    env = os.environ
    docs = Path(env.get("USERPROFILE", str(home))) / "Documents"
    roots = [
        docs / "EA SPORTS FC 26",
        docs / "EA SPORTS FC™ 26",
        docs / "FC 26",
        Path(env.get("LOCALAPPDATA", home / "AppData/Local")) / "EA SPORTS FC 26",
        Path(env.get("APPDATA", home / "AppData/Roaming")) / "EA SPORTS FC 26",
        Path(env.get("LOCALAPPDATA", home / "AppData/Local")) / "EA SPORTS FC™ 26",
        Path(env.get("PROGRAMDATA", "C:/ProgramData")) / "EA SPORTS FC 26",
    ]
    return [p for p in roots if p.exists()]


def inspect_save_root(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in {"screenshots", "cache", "logs"}]
        for name in names:
            p = Path(current) / name
            item = safe_stat(p)
            if item:
                item["relative_path"] = p.relative_to(root).as_posix()
                files.append(item)
            if len(files) >= 400:
                break
        if len(files) >= 400:
            break
    return {
        "root": sanitize_path(root),
        "files": files,
        "truncated": len(files) >= 400,
    }


def live_editor_info() -> list[dict[str, Any]]:
    candidates = [
        Path("C:/FC 26 Live Editor"),
        Path.home() / "Downloads/FC 26 Live Editor",
        Path.home() / "Desktop/FC 26 Live Editor",
    ]
    result = []
    for root in candidates:
        if not root.exists():
            continue
        files = []
        for name in ("version.json", "changelog.txt", "FC26LiveEditor.exe", "Launcher.exe"):
            p = root / name
            if p.exists():
                item = safe_stat(p)
                if item:
                    item["relative_path"] = name
                    files.append(item)
        result.append({"root": sanitize_path(root), "files": files})
    return result


def git_remote(repo_root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    remote = git_remote(repo_root)

    raw_candidates = registry_install_candidates() + common_install_candidates()
    installs: list[Path] = []
    seen = set()
    for candidate in raw_candidates:
        root = normalize_install_root(candidate)
        if root is None:
            continue
        key = str(root.resolve()).lower()
        if key not in seen:
            seen.add(key)
            installs.append(root)

    report: dict[str, Any] = {
        "schema": "fc26-ai-character-studio/bridge-probe-1.0",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "privacy": {
            "photos_read": False,
            "file_contents_uploaded": False,
            "paths_sanitized": True,
            "report_contains": "file names, relative paths, sizes, timestamps and hashes of reasonably sized technical files",
        },
        "system": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "machine": platform.machine(),
        },
        "repository": {
            "root": sanitize_path(repo_root),
            "remote": remote,
        },
        "game_installations": [],
        "save_locations": [],
        "live_editor": live_editor_info(),
    }

    for root in installs:
        print(f"[FC26] Analyse de {root} ...")
        report["game_installations"].append(inspect_tree(root))

    for root in save_candidates():
        print(f"[SAVE] Analyse de {root} ...")
        report["save_locations"].append(inspect_save_root(root))

    diagnostics = repo_root / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    output = diagnostics / "bridge_probe.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("============================================================")
    print(" FC26 BRIDGE PROBE TERMINE")
    print("============================================================")
    print(f"Installations détectées : {len(report['game_installations'])}")
    print(f"Emplacements de sauvegarde : {len(report['save_locations'])}")
    print(f"Rapport : {output}")
    if not report["game_installations"]:
        print("Aucune installation détectée automatiquement.")
        print("Le rapport reste utile : on ajoutera ton chemin exact au prochain passage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
