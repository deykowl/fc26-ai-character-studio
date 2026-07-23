from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "fc26-ai-character-studio/safe-career-load-test-1.6"
SAVE_TYPE = b"SaveType_Career\x00"
TEST_MORPH = "bs_skull_NN_SXX_F"
TEST_VALUE = 70


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_repo() -> Path:
    known = Path.home() / "Downloads/FC26_AI_Character_Studio_v1.0.1/FC26_Studio_Final_Work"
    if (known / ".git").exists():
        return known

    script = Path(__file__).resolve()
    for parent in [script.parent, *script.parents]:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError("Dossier Git du Studio introuvable.")


def load_json(path: Path) -> Any:
    if not path.exists():
        raise RuntimeError(f"Fichier requis absent : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def git_remote(repo: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def write_integer_field(
    data: bytearray,
    bit_offset: int,
    bit_depth: int,
    range_low: int,
    value: int,
) -> None:
    raw = value - range_low
    if raw < 0 or raw >= (1 << bit_depth):
        raise ValueError(
            f"Valeur {value} hors plage pour {bit_depth} bits, minimum {range_low}."
        )

    for bit_index in range(bit_depth):
        absolute_bit = bit_offset + bit_index
        byte_index = absolute_bit >> 3
        bit_in_byte = absolute_bit & 7
        mask = 1 << bit_in_byte
        if (raw >> bit_index) & 1:
            data[byte_index] |= mask
        else:
            data[byte_index] &= (~mask) & 0xFF


def read_integer_field(
    data: bytes | bytearray,
    bit_offset: int,
    bit_depth: int,
    range_low: int,
) -> int:
    raw = 0
    for bit_index in range(bit_depth):
        absolute_bit = bit_offset + bit_index
        byte_index = absolute_bit >> 3
        bit_in_byte = absolute_bit & 7
        if (data[byte_index] >> bit_in_byte) & 1:
            raw |= 1 << bit_index
    return raw + range_low


def find_field(save_map: dict[str, Any], morph_name: str) -> dict[str, Any]:
    for table_name in ("cp_skeletal", "cp_flesh", "cp_fat"):
        table = save_map.get("mapped_tables", {}).get(table_name, {})
        for field in table.get("fields", []):
            if field.get("name") == morph_name:
                return {**field, "table": table_name}
    raise RuntimeError(f"Champ {morph_name} introuvable dans la map.")


def unique_save_name(settings: Path) -> str:
    # Same pattern as FC26: CmPlrYYYYMMDDHHMMSSmmm
    base = "CmPlr" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
    candidate = base
    counter = 0
    while (settings / candidate).exists():
        counter += 1
        candidate = base[:-2] + f"{counter:02d}"
    return candidate


def report_path(repo: Path) -> Path:
    diagnostics = repo / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    return diagnostics / "safe_career_load_test.json"


def marker_path(repo: Path) -> Path:
    marker_dir = repo / "workspace/load_tests"
    marker_dir.mkdir(parents=True, exist_ok=True)
    return marker_dir / "active_test.json"


def prepare(repo: Path) -> int:
    settings = Path(os.environ["LOCALAPPDATA"]) / "EA SPORTS FC 26/settings"
    if not settings.exists():
        raise RuntimeError(f"Dossier FC26 introuvable : {settings}")

    marker = marker_path(repo)
    if marker.exists():
        existing = load_json(marker)
        existing_path = Path(existing.get("test_save_path", ""))
        if existing_path.exists():
            raise RuntimeError(
                "Une copie test existe déjà. Lance d'abord REMOVE_FC26_TEST_COPY.bat."
            )
        marker.unlink(missing_ok=True)

    save_map = load_json(repo / "diagnostics/cranium_save_map.json")
    writer_lab = load_json(repo / "diagnostics/cranium_writer_lab.json")

    source_name = save_map["clean_reference_save"]["name"]
    source = settings / source_name
    if not source.exists():
        raise RuntimeError(f"Sauvegarde source absente : {source}")

    expected_hash = (
        writer_lab.get("writer_copy_test", {}).get("source_sha256_after")
    )
    source_hash_before = sha256_file(source)
    if expected_hash and source_hash_before != expected_hash:
        raise RuntimeError(
            "La sauvegarde source a changé depuis la cartographie. "
            "Aucune copie test n'a été créée."
        )

    source_bytes = source.read_bytes()
    field = find_field(save_map, TEST_MORPH)

    bit_offset = int(field["absolute_file_bit_offset"])
    bit_depth = int(field["bit_depth"])
    range_low = int(field["range_low"])
    old_value = read_integer_field(
        source_bytes, bit_offset, bit_depth, range_low
    )

    modified = bytearray(source_bytes)
    write_integer_field(
        modified, bit_offset, bit_depth, range_low, TEST_VALUE
    )
    readback = read_integer_field(
        modified, bit_offset, bit_depth, range_low
    )
    if readback != TEST_VALUE:
        raise RuntimeError("Échec de relecture de la valeur test.")

    # The public FC26 FBCHUNKS writer treats the four bytes immediately
    # following SaveType_* as a validation/CRC field and zeroes them.
    save_type_pos = modified.find(SAVE_TYPE)
    if save_type_pos < 0:
        raise RuntimeError("SaveType_Career introuvable.")
    validation_pos = save_type_pos + len(SAVE_TYPE)
    old_validation_hex = bytes(modified[validation_pos:validation_pos + 4]).hex()
    modified[validation_pos:validation_pos + 4] = b"\x00\x00\x00\x00"

    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    test_root = repo / "workspace/load_tests" / timestamp
    test_root.mkdir(parents=True, exist_ok=True)

    backup = test_root / f"{source.name}.untouched_backup"
    shutil.copy2(source, backup)

    test_name = unique_save_name(settings)
    test_save = settings / test_name
    test_save.write_bytes(modified)

    source_hash_after = sha256_file(source)
    original_untouched = (
        source_hash_before == source_hash_after
        and source_hash_before == sha256_file(backup)
    )
    if not original_untouched:
        test_save.unlink(missing_ok=True)
        raise RuntimeError(
            "Contrôle de l'original échoué. La copie test a été supprimée."
        )

    test_hash = sha256_file(test_save)
    marker_data = {
        "schema": SCHEMA,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_save_path": str(source),
        "source_sha256": source_hash_before,
        "backup_path": str(backup),
        "test_save_path": str(test_save),
        "test_save_sha256": test_hash,
        "test_morph": TEST_MORPH,
        "old_value": old_value,
        "test_value": TEST_VALUE,
        "validation_field_old_hex": old_validation_hex,
        "validation_field_new_hex": "00000000",
    }
    marker.write_text(
        json.dumps(marker_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    report = {
        "schema": SCHEMA,
        "generated_utc": marker_data["created_utc"],
        "safety": {
            "source_save_modified": False,
            "game_process_opened": False,
            "game_files_modified": False,
            "anti_cheat_touched": False,
            "new_local_career_copy_created": True,
        },
        "repository": {"remote": git_remote(repo)},
        "source": {
            "name": source.name,
            "sha256_before": source_hash_before,
            "sha256_after": source_hash_after,
            "original_untouched": original_untouched,
            "backup_relative": backup.relative_to(repo).as_posix(),
        },
        "test_copy": {
            "name": test_save.name,
            "sha256": test_hash,
            "size": test_save.stat().st_size,
            "installed_in_fc26_settings": True,
            "test_morph": TEST_MORPH,
            "old_value": old_value,
            "new_value": TEST_VALUE,
            "readback_value": readback,
            "validation_field_old_hex": old_validation_hex,
            "validation_field_new_hex": "00000000",
            "game_load_test_performed": False,
        },
        "cleanup": {
            "marker_relative": marker.relative_to(repo).as_posix(),
            "remove_command": "python tools/fc26_safe_career_load_test.py cleanup",
        },
    }
    report_path(repo).write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print("============================================================")
    print(" COPIE TEST FC26 PREPAREE")
    print("============================================================")
    print(f"Original intact : {original_untouched}")
    print(f"Copie test : {test_save.name}")
    print(f"Réglage test : {TEST_MORPH} {old_value} -> {TEST_VALUE}")
    print()
    print("Lance maintenant FC26 normalement.")
    print("Dans Carrière Joueur, ouvre la sauvegarde la plus récente.")
    print("Après le test, ferme FC26 et lance REMOVE_FC26_TEST_COPY.bat.")
    return 0


def cleanup(repo: Path) -> int:
    marker = marker_path(repo)
    if not marker.exists():
        print("Aucune copie test active.")
        return 0

    data = load_json(marker)
    test_save = Path(data["test_save_path"])
    source = Path(data["source_save_path"])
    backup = Path(data["backup_path"])
    expected_source_hash = data["source_sha256"]

    removed = False
    if test_save.exists():
        test_save.unlink()
        removed = True

    source_ok = source.exists() and sha256_file(source) == expected_source_hash
    backup_ok = backup.exists() and sha256_file(backup) == expected_source_hash

    marker.unlink(missing_ok=True)

    existing_report = {}
    rpath = report_path(repo)
    if rpath.exists():
        existing_report = load_json(rpath)
    existing_report["cleanup_result"] = {
        "cleaned_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "test_copy_removed": removed,
        "test_copy_still_exists": test_save.exists(),
        "source_original_intact": source_ok,
        "backup_intact": backup_ok,
    }
    rpath.write_text(
        json.dumps(existing_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print("============================================================")
    print(" COPIE TEST SUPPRIMEE")
    print("============================================================")
    print(f"Copie supprimée : {removed}")
    print(f"Original intact : {source_ok}")
    print(f"Backup intact : {backup_ok}")
    return 0


def main() -> int:
    repo = find_repo()
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "prepare"
    if mode == "prepare":
        return prepare(repo)
    if mode == "cleanup":
        return cleanup(repo)
    raise SystemExit("Mode attendu : prepare ou cleanup")


if __name__ == "__main__":
    raise SystemExit(main())
