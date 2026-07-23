from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCHEMA = "fc26-ai-character-studio/cranium-save-map-1.4"
FBCHUNKS = b"FBCHUNKS"
DB_HEADER = b"\x44\x42\x00\x08\x00\x00\x00\x00"

TARGET_TABLES = {
    "cp_skeletal",
    "cp_flesh",
    "cp_fat",
    "createplayer",
    "players",
    "playerpronouns",
}

PLAYER_INTEREST = (
    "playerid", "firstname", "lastname", "commonname", "created", "createplayer",
    "gender", "sex", "female", "male", "pronoun",
    "height", "weight", "body", "physique", "shoulder",
    "hair", "beard", "facialhair", "eyebrow", "brow",
    "skin", "complexion", "tone", "eye", "iris",
    "head", "face", "facial", "morph", "cranium",
)

MAX_TABLES = 2000
MAX_FIELDS = 600
MAX_CREATED_PLAYER_ROWS = 64


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def ascii4(raw: bytes) -> str:
    return raw.decode("latin-1", errors="replace")


def compact(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def find_repo() -> Path:
    script = Path(__file__).resolve()
    for parent in [script.parent, *script.parents]:
        if (parent / ".git").exists():
            return parent
    known = Path.home() / "Downloads/FC26_AI_Character_Studio_v1.0.1/FC26_Studio_Final_Work"
    if (known / ".git").exists():
        return known
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


def load_metadata(repo: Path) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    xml_path = repo / "workspace/cache/fifa_ng_db-meta-fc26.xml"
    if not xml_path.exists():
        raise RuntimeError(
            "Métadonnées FC26 absentes. Relance d'abord le Career DB Probe v1.3."
        )

    root = ET.parse(xml_path).getroot()
    table_names: dict[str, str] = {}
    field_names: dict[str, str] = {}
    field_meta: dict[str, dict[str, Any]] = {}

    for table in root.findall("./table"):
        table_name = table.get("name") or ""
        table_short = table.get("shortname") or ""
        if table_short:
            table_names[table_short] = table_name
        for field in table.findall("./fields/field"):
            short = field.get("shortname") or ""
            if not short:
                continue
            name = field.get("name") or short
            low_raw = field.get("rangelow", "0")
            try:
                range_low = int(low_raw)
            except ValueError:
                range_low = 0
            field_names[short] = name
            field_meta[short] = {
                "name": name,
                "meta_type": field.get("type"),
                "depth": int(field.get("depth", "0")),
                "range_low": range_low,
            }
    return table_names, field_names, field_meta


def locate_db(raw: bytes) -> tuple[int, bytes]:
    candidates = []
    cursor = 0
    while True:
        offset = raw.find(DB_HEADER, cursor)
        if offset < 0:
            break
        cursor = offset + 1
        if offset + 24 > len(raw):
            continue
        size = u32(raw, offset + 8)
        tables = u32(raw, offset + 16)
        if 0 < tables <= MAX_TABLES and offset + size <= len(raw):
            index_end = offset + 24 + tables * 8 + 4
            if index_end <= offset + size:
                candidates.append((size, offset))
    if not candidates:
        raise RuntimeError("Base T3DB introuvable.")
    _, offset = max(candidates)
    size = u32(raw, offset + 8)
    return offset, raw[offset:offset + size]


def parse_tables(
    db: bytes,
    table_names: dict[str, str],
    field_names: dict[str, str],
    field_meta: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    table_count = u32(db, 16)
    index_pos = 24
    entries = []
    for _ in range(table_count):
        short = ascii4(db[index_pos:index_pos + 4])
        rel = u32(db, index_pos + 4)
        entries.append((short, rel))
        index_pos += 8
    index_pos += 4
    tables_start = index_pos

    result = {}
    for short, rel in entries:
        name = table_names.get(short, short)
        if name not in TARGET_TABLES:
            continue
        pos = tables_start + rel
        if pos + 36 > len(db):
            continue

        record_size = u32(db, pos + 4)
        valid_records = u16(db, pos + 18)
        field_count = db[pos + 24]
        fields_pos = pos + 36

        if field_count > MAX_FIELDS or fields_pos + field_count * 16 > len(db):
            continue

        fields = []
        for field_index in range(field_count):
            p = fields_pos + field_index * 16
            type_code = u32(db, p)
            bit_offset = u32(db, p + 4)
            field_short = ascii4(db[p + 8:p + 12])
            bit_depth = u32(db, p + 12)
            meta = field_meta.get(field_short, {})
            fields.append({
                "index": field_index,
                "short": field_short,
                "name": field_names.get(field_short, field_short),
                "type_code": type_code,
                "meta_type": meta.get("meta_type"),
                "bit_offset": bit_offset,
                "bit_depth": bit_depth,
                "range_low": int(meta.get("range_low", 0)),
            })

        records_start = fields_pos + field_count * 16
        result[name] = {
            "short": short,
            "table_header_in_db": pos,
            "record_size": record_size,
            "valid_records": valid_records,
            "field_count": field_count,
            "records_start_in_db": records_start,
            "fields": fields,
        }
    return result


def read_value(db: bytes, record_start: int, field: dict[str, Any]) -> Any:
    type_code = int(field["type_code"])
    bit_offset = int(field["bit_offset"])
    bit_depth = int(field["bit_depth"])
    range_low = int(field.get("range_low", 0))
    byte_pos = record_start + (bit_offset >> 3)

    if byte_pos < 0 or byte_pos >= len(db):
        return None

    if type_code == 3 and 0 < bit_depth <= 64:
        value = 0
        for i in range(bit_depth):
            source_bit = bit_offset + i
            source_byte = record_start + (source_bit >> 3)
            if source_byte >= len(db):
                return None
            if (db[source_byte] >> (source_bit & 7)) & 1:
                value |= 1 << i
        return value + range_low

    if type_code == 4 and byte_pos + 4 <= len(db):
        value = struct.unpack_from("<f", db, byte_pos)[0]
        if value != value or abs(value) > 1e12:
            return None
        return round(float(value), 7)

    if type_code == 0:
        length = max(0, bit_depth // 8)
        raw = db[byte_pos:byte_pos + length].split(b"\x00", 1)[0]
        text = raw.decode("utf-8", errors="ignore")
        return "<string_redacted>" if text else ""

    # Preserve unknown field types as a short non-sensitive hex sample.
    byte_len = max(1, min(8, (bit_depth + 7) // 8))
    return {"raw_hex": db[byte_pos:byte_pos + byte_len].hex()}


def field_entry_with_value(
    db: bytes,
    db_file_offset: int,
    table: dict[str, Any],
    record_index: int,
    field: dict[str, Any],
) -> dict[str, Any]:
    record_start = int(table["records_start_in_db"]) + record_index * int(table["record_size"])
    absolute_file_bit = (
        (db_file_offset + record_start) * 8 + int(field["bit_offset"])
    )
    return {
        **field,
        "value": read_value(db, record_start, field),
        "absolute_file_bit_offset": absolute_file_bit,
        "absolute_file_byte_offset": absolute_file_bit // 8,
        "bit_in_byte": absolute_file_bit & 7,
    }


def select_clean_save(parsed: list[dict[str, Any]]) -> dict[str, Any]:
    scored = []
    for item in parsed:
        tables = item["tables"]
        counts = [
            int(tables.get(name, {}).get("valid_records", 999999))
            for name in ("cp_skeletal", "cp_flesh", "cp_fat", "createplayer")
        ]
        score = sum(counts)
        scored.append((score, item))
    return min(scored, key=lambda pair: pair[0])[1]


def player_fields_of_interest(table: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for field in table["fields"]:
        key = compact(field["name"])
        if any(token in key for token in PLAYER_INTEREST):
            selected.append(field)
    return selected


def collect_created_player_rows(
    db: bytes,
    table: dict[str, Any],
    db_file_offset: int,
) -> list[dict[str, Any]]:
    interesting = player_fields_of_interest(table)
    created_fields = [
        field for field in interesting
        if "created" in compact(field["name"]) or "createplayer" in compact(field["name"])
    ]
    id_fields = [
        field for field in interesting
        if compact(field["name"]) in {"playerid", "createplayerid"}
        or compact(field["name"]).endswith("playerid")
    ]

    rows = []
    max_rows = min(int(table["valid_records"]), 50000)
    for record_index in range(max_rows):
        record_start = int(table["records_start_in_db"]) + record_index * int(table["record_size"])
        is_created = False
        created_values = {}
        for field in created_fields:
            value = read_value(db, record_start, field)
            created_values[field["name"]] = value
            if isinstance(value, (int, float)) and value != 0:
                is_created = True

        if not is_created:
            continue

        values = {}
        for field in interesting:
            value = read_value(db, record_start, field)
            if field in id_fields or value not in (None, 0, "", "<string_redacted>"):
                values[field["name"]] = value

        rows.append({
            "record_index": record_index,
            "created_flags": created_values,
            "values": values,
        })
        if len(rows) >= MAX_CREATED_PLAYER_ROWS:
            break
    return rows


def table_fingerprint(db: bytes, table: dict[str, Any], record_index: int) -> str:
    start = int(table["records_start_in_db"]) + record_index * int(table["record_size"])
    end = start + int(table["record_size"])
    return hashlib.sha256(db[start:end]).hexdigest()


def main() -> int:
    repo = find_repo()
    table_names, field_names, field_meta = load_metadata(repo)
    settings = Path(os.environ["LOCALAPPDATA"]) / "EA SPORTS FC 26/settings"
    saves = sorted(
        [p for p in settings.glob("CmPlr*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    if not saves:
        raise RuntimeError("Aucune sauvegarde Carrière Joueur trouvée.")

    parsed = []
    for path in saves:
        print(f"Lecture : {path.name}")
        raw = path.read_bytes()
        db_offset, db = locate_db(raw)
        tables = parse_tables(db, table_names, field_names, field_meta)
        parsed.append({
            "path": path,
            "raw": raw,
            "db_offset": db_offset,
            "db": db,
            "tables": tables,
        })

    clean = select_clean_save(parsed)
    clean_path: Path = clean["path"]
    clean_db: bytes = clean["db"]
    clean_db_offset = int(clean["db_offset"])
    clean_tables: dict[str, dict[str, Any]] = clean["tables"]

    mapped_tables = {}
    for name in ("cp_skeletal", "cp_flesh", "cp_fat", "createplayer"):
        table = clean_tables.get(name)
        if not table:
            continue
        record_index = 0
        mapped_tables[name] = {
            "short": table["short"],
            "record_size": table["record_size"],
            "valid_records": table["valid_records"],
            "field_count": table["field_count"],
            "record_index": record_index,
            "record_sha256": table_fingerprint(clean_db, table, record_index),
            "fields": [
                field_entry_with_value(
                    clean_db, clean_db_offset, table, record_index, field
                )
                for field in table["fields"]
            ],
        }

    players_summary = {}
    players_table = clean_tables.get("players")
    if players_table:
        players_summary = {
            "record_size": players_table["record_size"],
            "valid_records": players_table["valid_records"],
            "fields_of_interest": player_fields_of_interest(players_table),
            "created_player_rows": collect_created_player_rows(
                clean_db, players_table, clean_db_offset
            ),
        }

    pronouns_summary = {}
    pronouns_table = clean_tables.get("playerpronouns")
    if pronouns_table:
        records = []
        for record_index in range(min(int(pronouns_table["valid_records"]), 16)):
            records.append({
                "record_index": record_index,
                "values": {
                    field["name"]: read_value(
                        clean_db,
                        int(pronouns_table["records_start_in_db"])
                        + record_index * int(pronouns_table["record_size"]),
                        field,
                    )
                    for field in pronouns_table["fields"]
                },
            })
        pronouns_summary = {
            "fields": pronouns_table["fields"],
            "records": records,
        }

    save_inventory = []
    for item in parsed:
        counts = {
            name: int(item["tables"].get(name, {}).get("valid_records", 0))
            for name in ("cp_skeletal", "cp_flesh", "cp_fat", "createplayer")
        }
        save_inventory.append({
            "name": item["path"].name,
            "modified_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(item["path"].stat().st_mtime),
            ),
            "db_offset": item["db_offset"],
            "counts": counts,
        })

    layer_counts = {
        name: int(mapped_tables.get(name, {}).get("field_count", 0))
        for name in ("cp_skeletal", "cp_flesh", "cp_fat")
    }

    report = {
        "schema": SCHEMA,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety": {
            "read_only": True,
            "game_process_opened": False,
            "game_files_modified": False,
            "save_files_modified": False,
            "anti_cheat_touched": False,
            "raw_saves_uploaded": False,
        },
        "repository": {"remote": git_remote(repo)},
        "clean_reference_save": {
            "name": clean_path.name,
            "reason": "minimum total create-player records",
            "db_file_offset": clean_db_offset,
        },
        "save_inventory": save_inventory,
        "layer_field_counts": layer_counts,
        "layer_total_fields": sum(layer_counts.values()),
        "mapped_tables": mapped_tables,
        "players": players_summary,
        "playerpronouns": pronouns_summary,
        "writer_status": {
            "field_addresses_mapped": bool(mapped_tables),
            "write_test_performed": False,
            "next_required_step": (
                "Create a duplicate save, change exactly one FC26 creator control, "
                "save again, then compare the two files to validate encoding and checksums."
            ),
        },
    }

    diagnostics = repo / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    output = diagnostics / "cranium_save_map.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("============================================================")
    print(" FC26 CRANIUM SAVE MAP TERMINE")
    print("============================================================")
    print(f"Sauvegarde de référence : {clean_path.name}")
    print(f"Champs Cranium cartographiés : {sum(layer_counts.values())}")
    print(f"Rapport : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
