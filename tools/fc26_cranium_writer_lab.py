from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA = "fc26-ai-character-studio/cranium-writer-lab-1.5"


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


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

    script_path = Path(__file__).resolve()
    for parent in [script_path.parent, *script_path.parents]:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError("Dossier Git du Studio introuvable.")


def git_remote(repo: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def load_json(path: Path) -> Any:
    if not path.exists():
        raise RuntimeError(f"Fichier requis absent : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def flatten_studio_axes(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    axes = []
    for control in controls:
        for axis in control.get("axes", []):
            morph = str(axis.get("morph", "")).strip()
            if not morph:
                continue
            axes.append({
                "control_index": control.get("index"),
                "control": control.get("control"),
                "display_name": control.get("display_name"),
                "category": control.get("category"),
                "region": control.get("region"),
                "layer": control.get("layer"),
                "axis": axis.get("axis"),
                "slot": axis.get("slot"),
                "morph": morph,
                "normalized_morph": normalize(morph),
                "morph_index": axis.get("morph_index"),
                "positive_scale": axis.get("positive_scale"),
                "negative_scale": axis.get("negative_scale"),
            })
    return axes


def flatten_save_fields(save_map: dict[str, Any]) -> list[dict[str, Any]]:
    fields = []
    for table_name in ("cp_skeletal", "cp_flesh", "cp_fat"):
        table = save_map.get("mapped_tables", {}).get(table_name, {})
        for field in table.get("fields", []):
            fields.append({
                **field,
                "table": table_name,
                "normalized_name": normalize(str(field.get("name", ""))),
            })
    return fields


def build_mapping(
    studio_axes: list[dict[str, Any]],
    save_fields: list[dict[str, Any]],
) -> dict[str, Any]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for field in save_fields:
        by_name[field["normalized_name"]].append(field)

    matched = []
    unmatched_axes = []
    ambiguous = []

    for axis in studio_axes:
        candidates = by_name.get(axis["normalized_morph"], [])
        if len(candidates) == 1:
            field = candidates[0]
            matched.append({
                **axis,
                "save_table": field["table"],
                "save_short": field.get("short"),
                "save_name": field.get("name"),
                "save_value": field.get("value"),
                "bit_depth": field.get("bit_depth"),
                "range_low": field.get("range_low"),
                "absolute_file_bit_offset": field.get("absolute_file_bit_offset"),
                "absolute_file_byte_offset": field.get("absolute_file_byte_offset"),
                "bit_in_byte": field.get("bit_in_byte"),
            })
        elif len(candidates) > 1:
            ambiguous.append({
                "axis": axis,
                "candidates": [
                    {
                        "table": candidate["table"],
                        "name": candidate.get("name"),
                        "short": candidate.get("short"),
                    }
                    for candidate in candidates
                ],
            })
        else:
            unmatched_axes.append(axis)

    matched_save_keys = {
        (item["save_table"], item["save_short"]) for item in matched
    }
    unmatched_save_fields = [
        field for field in save_fields
        if (field["table"], field.get("short")) not in matched_save_keys
    ]

    return {
        "studio_axis_entries": len(studio_axes),
        "studio_unique_morphs": len({axis["normalized_morph"] for axis in studio_axes}),
        "save_fields": len(save_fields),
        "exact_matches": len(matched),
        "unique_matched_save_fields": len(matched_save_keys),
        "ambiguous_axes": len(ambiguous),
        "unmatched_studio_axes": len(unmatched_axes),
        "unmatched_save_fields": len(unmatched_save_fields),
        "by_layer": dict(Counter(item["save_table"] for item in matched)),
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched_axes": unmatched_axes,
        "unmatched_save_fields_list": unmatched_save_fields,
    }


def read_integer_field(data: bytes | bytearray, field: dict[str, Any]) -> int:
    bit_offset = int(field["absolute_file_bit_offset"])
    bit_depth = int(field["bit_depth"])
    range_low = int(field["range_low"])
    raw = 0
    for bit_index in range(bit_depth):
        absolute_bit = bit_offset + bit_index
        byte_index = absolute_bit >> 3
        bit_in_byte = absolute_bit & 7
        if byte_index >= len(data):
            raise RuntimeError("Champ hors fichier.")
        if (data[byte_index] >> bit_in_byte) & 1:
            raw |= 1 << bit_index
    return raw + range_low


def write_integer_field(
    data: bytearray,
    field: dict[str, Any],
    desired_value: int,
) -> None:
    bit_offset = int(field["absolute_file_bit_offset"])
    bit_depth = int(field["bit_depth"])
    range_low = int(field["range_low"])
    raw = desired_value - range_low
    if raw < 0 or raw >= (1 << bit_depth):
        raise ValueError(
            f"Valeur {desired_value} impossible pour {bit_depth} bits "
            f"avec minimum {range_low}."
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


def choose_test_field(matches: list[dict[str, Any]]) -> dict[str, Any]:
    preferred = []
    fallback = []
    for item in matches:
        if item.get("bit_depth") != 8 or item.get("range_low") != -127:
            continue
        value = item.get("save_value")
        if not isinstance(value, int) or not (-126 <= value <= 126):
            continue
        fallback.append(item)
        name = str(item.get("save_name", ""))
        if any(token in name for token in ("skull", "forehead", "chin", "orbits")):
            preferred.append(item)
    candidates = preferred or fallback
    if not candidates:
        raise RuntimeError("Aucun champ sûr trouvé pour le test synthétique.")
    return candidates[0]


def compact_match(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "control": item.get("control"),
        "display_name": item.get("display_name"),
        "layer": item.get("layer"),
        "axis": item.get("axis"),
        "morph": item.get("morph"),
        "save_table": item.get("save_table"),
        "save_short": item.get("save_short"),
        "save_name": item.get("save_name"),
        "save_value": item.get("save_value"),
        "bit_depth": item.get("bit_depth"),
        "range_low": item.get("range_low"),
    }


def main() -> int:
    repo = find_repo()
    save_map_path = repo / "diagnostics/cranium_save_map.json"
    controls_path = repo / "app/static/assets/shape_controls.json"

    save_map = load_json(save_map_path)
    controls = load_json(controls_path)
    if not isinstance(controls, list):
        raise RuntimeError("shape_controls.json n'est pas une liste.")

    studio_axes = flatten_studio_axes(controls)
    save_fields = flatten_save_fields(save_map)
    mapping = build_mapping(studio_axes, save_fields)

    generated_dir = repo / "workspace/generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    full_mapping_path = generated_dir / "cranium_axis_link.json"
    full_mapping_path.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "generated_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "summary": {
                    key: value for key, value in mapping.items()
                    if key not in {
                        "matched", "ambiguous",
                        "unmatched_axes", "unmatched_save_fields_list"
                    }
                },
                "matches": [compact_match(item) | {
                    "absolute_file_bit_offset": item.get("absolute_file_bit_offset"),
                    "absolute_file_byte_offset": item.get("absolute_file_byte_offset"),
                    "bit_in_byte": item.get("bit_in_byte"),
                    "morph_index": item.get("morph_index"),
                    "positive_scale": item.get("positive_scale"),
                    "negative_scale": item.get("negative_scale"),
                } for item in mapping["matched"]],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reference_name = save_map["clean_reference_save"]["name"]
    settings = Path(os.environ["LOCALAPPDATA"]) / "EA SPORTS FC 26/settings"
    source_save = settings / reference_name
    if not source_save.exists():
        raise RuntimeError(f"Sauvegarde de référence absente : {source_save}")

    source_before_hash = sha256_file(source_save)
    source_bytes = source_save.read_bytes()

    field = choose_test_field(mapping["matched"])
    map_value = int(field["save_value"])
    actual_value = read_integer_field(source_bytes, field)
    if actual_value != map_value:
        raise RuntimeError(
            f"La sauvegarde a changé depuis la map : "
            f"{field['save_name']} vaut {actual_value}, map={map_value}."
        )

    new_value = actual_value + 1 if actual_value < 126 else actual_value - 1

    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    lab_dir = repo / "workspace/write_tests" / timestamp
    lab_dir.mkdir(parents=True, exist_ok=True)

    original_copy = lab_dir / f"{source_save.name}.original_copy"
    modified_copy = lab_dir / f"{source_save.name}.synthetic_test_copy"
    shutil.copy2(source_save, original_copy)

    modified = bytearray(source_bytes)
    write_integer_field(modified, field, new_value)
    modified_copy.write_bytes(modified)

    readback = read_integer_field(modified, field)
    changed_positions = [
        index for index, (left, right) in enumerate(zip(source_bytes, modified))
        if left != right
    ]

    source_after_hash = sha256_file(source_save)
    original_untouched = (
        source_before_hash == source_after_hash
        and source_before_hash == sha256_file(original_copy)
    )

    expected_byte_span_start = int(field["absolute_file_bit_offset"]) // 8
    expected_byte_span_end = (
        int(field["absolute_file_bit_offset"])
        + int(field["bit_depth"]) - 1
    ) // 8
    changes_inside_target = all(
        expected_byte_span_start <= position <= expected_byte_span_end
        for position in changed_positions
    )

    createplayer = save_map.get("mapped_tables", {}).get("createplayer", {})
    players = save_map.get("players", {})
    player_fields = {
        item.get("name"): {
            "bit_depth": item.get("bit_depth"),
            "range_low": item.get("range_low"),
        }
        for item in players.get("fields_of_interest", [])
        if item.get("name") in {
            "gender", "height", "weight", "bodytypecode",
            "hairtypecode", "haircolorcode", "hairstylecode",
            "facialhairtypecode", "facialhaircolorcode",
            "eyebrowcode", "eyecolorcode", "skintypecode",
            "skintonecode", "skincomplexion", "skinsurfacepack",
            "headtypecode", "headassetid", "headclasscode",
            "headvariation", "hashighqualityhead", "eyedetail",
        }
    }

    report = {
        "schema": SCHEMA,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety": {
            "source_save_modified": False,
            "game_process_opened": False,
            "game_files_modified": False,
            "anti_cheat_touched": False,
            "test_copy_written_outside_game_save_folder": True,
        },
        "repository": {"remote": git_remote(repo)},
        "mapping": {
            key: value for key, value in mapping.items()
            if key not in {
                "matched", "ambiguous",
                "unmatched_axes", "unmatched_save_fields_list"
            }
        },
        "mapping_samples": [
            compact_match(item) for item in mapping["matched"][:20]
        ],
        "unmatched_studio_axis_names": [
            item.get("morph") for item in mapping["unmatched_axes"]
        ],
        "unmatched_save_field_names": [
            {
                "table": item.get("table"),
                "name": item.get("name"),
                "short": item.get("short"),
                "value": item.get("value"),
                "bit_depth": item.get("bit_depth"),
                "range_low": item.get("range_low"),
            }
            for item in mapping["unmatched_save_fields_list"]
        ],
        "writer_copy_test": {
            "source_save": source_save.name,
            "source_sha256_before": source_before_hash,
            "source_sha256_after": source_after_hash,
            "source_original_untouched": original_untouched,
            "test_field": compact_match(field),
            "old_value": actual_value,
            "requested_new_value": new_value,
            "readback_value": readback,
            "readback_matches": readback == new_value,
            "changed_byte_count": len(changed_positions),
            "changed_byte_offsets": changed_positions,
            "all_changes_inside_target_field": changes_inside_target,
            "file_size_unchanged": len(source_bytes) == len(modified),
            "original_copy_relative": original_copy.relative_to(repo).as_posix(),
            "modified_copy_relative": modified_copy.relative_to(repo).as_posix(),
            "modified_copy_installed_into_fc26": False,
        },
        "appearance_player_fields": player_fields,
        "createplayer_field_count": createplayer.get("field_count"),
        "next_gate": {
            "safe_copy_writer_ready": (
                original_untouched
                and readback == new_value
                and changes_inside_target
                and len(source_bytes) == len(modified)
            ),
            "game_load_test_performed": False,
            "reason": (
                "The synthetic copy is deliberately not installed into FC26. "
                "A throwaway career and full automatic backup/restore flow are "
                "required before a real load test."
            ),
        },
    }

    diagnostics = repo / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    report_path = diagnostics / "cranium_writer_lab.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print("============================================================")
    print(" FC26 CRANIUM WRITER LAB TERMINE")
    print("============================================================")
    print(f"Axes Studio : {mapping['studio_axis_entries']}")
    print(f"Correspondances exactes : {mapping['exact_matches']}")
    print(f"Champs sauvegarde non relies : {mapping['unmatched_save_fields']}")
    print(f"Valeur test : {actual_value} -> {new_value} -> {readback}")
    print(f"Octets modifies dans la copie : {len(changed_positions)}")
    print(f"Original intact : {original_untouched}")
    print(f"Rapport : {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
