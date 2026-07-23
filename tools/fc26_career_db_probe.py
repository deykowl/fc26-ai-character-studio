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
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCHEMA = "fc26-ai-character-studio/career-db-probe-1.3"
FBCHUNKS = b"FBCHUNKS"
DB_HEADER = b"\x44\x42\x00\x08\x00\x00\x00\x00"
META_URL = (
    "https://raw.githubusercontent.com/INSANE0777/"
    "Fc26-live-editor-mcp/main/src/fc26_mcp/data/fifa_ng_db-meta-fc26.xml"
)

APPEARANCE_TOKENS = {
    "appearance", "visual", "vpro", "virtualpro", "createplayer", "createdplayer",
    "skeletal", "flesh", "fat", "secondaryform", "craniumcontrol",
    "head", "face", "facial", "cranium", "morph", "preset",
    "skin", "complexion", "tone", "color", "colour",
    "hair", "hairstyle", "beard", "facialhair", "brow", "eyebrow",
    "eye", "iris", "nose", "jaw", "chin", "mouth", "lip", "cheek", "ear",
    "gender", "female", "male", "sex",
    "body", "bodytype", "height", "weight", "shoulder", "torso", "arm", "leg",
    "neck", "physique", "build",
}

IDENTITY_TOKENS = {
    "playerid", "firstname", "lastname", "commonname", "teamid", "userid",
}

MAX_TABLES = 2000
MAX_FIELDS_PER_TABLE = 500
MAX_CANDIDATE_TABLES = 100
MAX_RECORD_SAMPLES = 32


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def custom_crc32(payload: bytes) -> int:
    # Frostbite FBCHUNKS uses standard ZIP CRC32 with 0x12345678 as initial value.
    return binascii.crc32(payload, 0x12345678) & 0xFFFFFFFF


def read_u16(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def safe_ascii(raw: bytes) -> str:
    return raw.decode("latin-1", errors="replace")


def compact_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def is_appearance_name(*values: str | None) -> bool:
    merged = " ".join(compact_name(value) for value in values)
    return any(token in merged for token in APPEARANCE_TOKENS)


def find_repo() -> Path:
    script = Path(__file__).resolve()
    for parent in [script.parent, *script.parents]:
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


def load_metadata(repo: Path) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]], str]:
    cache_dir = repo / "workspace" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "fifa_ng_db-meta-fc26.xml"

    source = "cache"
    if not cache_path.exists() or cache_path.stat().st_size < 100_000:
        source = META_URL
        try:
            print("Téléchargement des métadonnées FC26 (1,5 Mo)...")
            request = urllib.request.Request(
                META_URL,
                headers={"User-Agent": "FC26-AI-Character-Studio/1.2"},
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
            if len(payload) < 100_000:
                raise RuntimeError("fichier de métadonnées incomplet")
            cache_path.write_bytes(payload)
        except Exception as exc:
            print(f"Métadonnées indisponibles : {exc}")
            return {}, {}, {}, "unavailable"

    table_names: dict[str, str] = {}
    field_names: dict[str, str] = {}
    field_meta: dict[str, dict[str, Any]] = {}
    try:
        root = ET.parse(cache_path).getroot()
        for table in root.findall("./table"):
            table_name = table.get("name") or ""
            table_short = table.get("shortname") or ""
            if table_short:
                table_names[table_short] = table_name
            for field in table.findall("./fields/field"):
                short = field.get("shortname") or ""
                name = field.get("name") or short
                if not short:
                    continue
                field_names[short] = name
                field_meta[short] = {
                    "name": name,
                    "type": field.get("type"),
                    "depth": int(field.get("depth", "0")),
                    "range_low": int(field.get("rangelow", "0"))
                    if (field.get("rangelow", "0").lstrip("-").isdigit())
                    else 0,
                }
        return table_names, field_names, field_meta, source
    except Exception as exc:
        print(f"Lecture XML impossible : {exc}")
        return {}, {}, {}, "invalid"


def parse_outer_header(raw: bytes) -> dict[str, Any]:
    if len(raw) < 0x1A or raw[:8] != FBCHUNKS:
        raise ValueError("Le fichier ne commence pas par FBCHUNKS.")

    version = read_u16(raw, 8)
    header_size = read_u32(raw, 10)
    data_size = read_u32(raw, 14)
    header_start = 18
    header_end = header_start + header_size
    data_start = header_end
    data_end = data_start + data_size

    if header_end > len(raw):
        raise ValueError("Taille d'en-tête FBCHUNKS invalide.")
    if data_start + 4 > len(raw):
        raise ValueError("Zone de données FBCHUNKS invalide.")

    header_crc_stored = read_u32(raw, header_start)
    header_crc_actual = custom_crc32(raw[header_start + 4:header_end])

    data_crc_stored = read_u32(raw, data_start)
    bounded_data_end = min(data_end, len(raw))
    data_crc_actual = custom_crc32(raw[data_start + 4:bounded_data_end])

    return {
        "version": version,
        "header_size": header_size,
        "data_size_declared": data_size,
        "header_start": header_start,
        "header_end": header_end,
        "data_start": data_start,
        "data_end_declared": data_end,
        "file_size": len(raw),
        "sizes_fit_file": data_end <= len(raw),
        "header_crc": {
            "stored_hex": f"{header_crc_stored:08x}",
            "actual_hex": f"{header_crc_actual:08x}",
            "matches": header_crc_stored == header_crc_actual,
        },
        "data_crc": {
            "stored_hex": f"{data_crc_stored:08x}",
            "actual_hex": f"{data_crc_actual:08x}",
            "matches": data_crc_stored == data_crc_actual,
            "calculated_until": bounded_data_end,
        },
        "header_magic_preview": safe_ascii(raw[header_start + 4:header_start + 20]),
        "data_magic_preview_hex": raw[data_start + 4:data_start + 36].hex(),
    }


def find_db_candidates(raw: bytes) -> list[dict[str, Any]]:
    candidates = []
    cursor = 0
    while True:
        offset = raw.find(DB_HEADER, cursor)
        if offset < 0:
            break
        cursor = offset + 1
        candidate: dict[str, Any] = {"offset": offset}
        try:
            db_size = read_u32(raw, offset + 8)
            table_count = read_u32(raw, offset + 16)
            index_end = offset + 24 + table_count * 8 + 4
            candidate.update({
                "db_size": db_size,
                "table_count": table_count,
                "db_fits_file": offset + db_size <= len(raw),
                "index_fits_db": table_count <= MAX_TABLES and index_end <= min(len(raw), offset + db_size),
                "header_hex_32": raw[offset:offset + 32].hex(),
            })
            if table_count > MAX_TABLES:
                candidate["rejected_reason"] = "table_count_too_large"
        except Exception as exc:
            candidate["error"] = str(exc)
        candidates.append(candidate)
        if len(candidates) >= 20:
            break
    return candidates


def parse_table_schema(
    db_data: bytes,
    table_entry: dict[str, Any],
    tables_start: int,
    field_names: dict[str, str],
    field_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    table_offset = int(table_entry["offset"])
    pos = tables_start + table_offset
    result = dict(table_entry)
    result["absolute_in_db"] = pos

    try:
        if pos < 0 or pos + 36 > len(db_data):
            raise ValueError("offset hors base")

        unknown0 = read_u32(db_data, pos)
        record_size = read_u32(db_data, pos + 4)
        valid_records = read_u16(db_data, pos + 18)
        fields_count = db_data[pos + 24]
        fields_pos = pos + 36

        if record_size <= 0 or record_size > 1_000_000:
            raise ValueError(f"record_size suspect: {record_size}")
        if fields_count > MAX_FIELDS_PER_TABLE:
            raise ValueError(f"fields_count suspect: {fields_count}")
        if fields_pos + fields_count * 16 > len(db_data):
            raise ValueError("descripteurs de champs hors base")

        fields = []
        for _ in range(fields_count):
            field_type = read_u32(db_data, fields_pos)
            bit_offset = read_u32(db_data, fields_pos + 4)
            short = safe_ascii(db_data[fields_pos + 8:fields_pos + 12])
            bit_depth = read_u32(db_data, fields_pos + 12)
            full = field_names.get(short, short)
            meta = field_meta.get(short, {})
            fields.append({
                "short": short,
                "name": full,
                "type_code": field_type,
                "meta_type": meta.get("type"),
                "bit_offset": bit_offset,
                "bit_depth": bit_depth,
                "range_low": meta.get("range_low", 0),
                "appearance_related": is_appearance_name(short, full),
            })
            fields_pos += 16

        appearance_fields = [field for field in fields if field["appearance_related"]]
        identity_fields = [
            field for field in fields
            if any(token in compact_name(field["name"]) for token in IDENTITY_TOKENS)
        ]

        result.update({
            "parse_ok": True,
            "unknown0": unknown0,
            "record_size": record_size,
            "valid_records": valid_records,
            "fields_count": fields_count,
            "records_start": fields_pos,
            "appearance_fields": appearance_fields,
            "identity_fields": identity_fields,
            "all_field_names": [field["name"] for field in fields],
        })
        return result
    except Exception as exc:
        result.update({"parse_ok": False, "error": str(exc)})
        return result


def read_field_value(
    db_data: bytes,
    record_pos: int,
    field: dict[str, Any],
) -> Any:
    field_type = int(field["type_code"])
    bit_offset = int(field["bit_offset"])
    bit_depth = int(field["bit_depth"])
    byte_offset = record_pos + (bit_offset >> 3)

    if byte_offset < 0 or byte_offset >= len(db_data):
        return None

    if field_type == 0:
        length = max(0, bit_depth >> 3)
        end = min(len(db_data), byte_offset + length)
        raw = db_data[byte_offset:end]
        raw = raw.split(b"\x00", 1)[0]
        text = raw.decode("utf-8", errors="ignore")
        # No user names are exported; only appearance strings are kept.
        return text[:80] if is_appearance_name(text) else "<redacted_string>"

    if field_type == 4 and byte_offset + 4 <= len(db_data):
        value = struct.unpack_from("<f", db_data, byte_offset)[0]
        if value != value or abs(value) > 1e9:
            return None
        return round(float(value), 6)

    if field_type == 3 and 0 < bit_depth <= 64:
        value = 0
        for bit_index in range(bit_depth):
            source_bit = bit_offset + bit_index
            source_byte = record_pos + (source_bit >> 3)
            if source_byte >= len(db_data):
                break
            if (db_data[source_byte] >> (source_bit & 7)) & 1:
                value |= 1 << bit_index
        return value + int(field.get("range_low", 0))

    return None


def add_record_samples(db_data: bytes, table: dict[str, Any]) -> None:
    if not table.get("parse_ok"):
        return
    fields = table.get("appearance_fields", [])
    if not fields:
        return

    valid_records = min(int(table.get("valid_records", 0)), MAX_RECORD_SAMPLES)
    record_size = int(table["record_size"])
    records_start = int(table["records_start"])
    samples = []
    for record_index in range(valid_records):
        record_pos = records_start + record_index * record_size
        if record_pos + record_size > len(db_data):
            break
        values = {}
        for field in fields:
            value = read_field_value(db_data, record_pos, field)
            if value is not None:
                values[field["name"]] = value
        if values:
            samples.append({"record_index": record_index, "appearance_values": values})
    table["appearance_record_samples"] = samples


def inspect_database(
    raw: bytes,
    table_names: dict[str, str],
    field_names: dict[str, str],
    field_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    candidates = find_db_candidates(raw)
    valid = [
        candidate for candidate in candidates
        if candidate.get("index_fits_db") and candidate.get("db_fits_file")
    ]
    if not valid:
        return {
            "found": False,
            "candidates": candidates,
            "reason": "no_valid_t3db_candidate",
        }

    # Prefer the largest valid DB.
    selected = max(valid, key=lambda item: int(item.get("db_size", 0)))
    db_offset = int(selected["offset"])
    db_size = int(selected["db_size"])
    table_count = int(selected["table_count"])
    db_data = raw[db_offset:db_offset + db_size]

    index_pos = 24
    entries = []
    for _ in range(table_count):
        short = safe_ascii(db_data[index_pos:index_pos + 4])
        relative_offset = read_u32(db_data, index_pos + 4)
        entries.append({
            "short": short,
            "name": table_names.get(short, short),
            "offset": relative_offset,
        })
        index_pos += 8
    index_pos += 4
    tables_start = index_pos

    parsed_tables = [
        parse_table_schema(db_data, entry, tables_start, field_names, field_meta)
        for entry in entries
    ]

    candidate_tables = [
        table for table in parsed_tables
        if table.get("parse_ok") and (
            is_appearance_name(table.get("short"), table.get("name"))
            or bool(table.get("appearance_fields"))
            or table.get("name") in {"cp_skeletal", "cp_flesh", "cp_fat", "createplayer", "players", "playerpronouns"}
        )
    ][:MAX_CANDIDATE_TABLES]

    for table in candidate_tables:
        add_record_samples(db_data, table)

    return {
        "found": True,
        "all_candidates": candidates,
        "selected": selected,
        "db_sha256": hashlib.sha256(db_data).hexdigest(),
        "tables_start": tables_start,
        "table_count": table_count,
        "parsed_table_count": sum(1 for table in parsed_tables if table.get("parse_ok")),
        "failed_table_count": sum(1 for table in parsed_tables if not table.get("parse_ok")),
        "table_catalog": [
            {
                "short": table.get("short"),
                "name": table.get("name"),
                "record_size": table.get("record_size"),
                "valid_records": table.get("valid_records"),
                "fields_count": table.get("fields_count"),
                "parse_ok": table.get("parse_ok"),
            }
            for table in parsed_tables
        ],
        "appearance_candidate_tables": candidate_tables,
    }


def inspect_save(
    path: Path,
    table_names: dict[str, str],
    field_names: dict[str, str],
    field_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "name": path.name,
        "size": len(raw),
        "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
        "sha256": sha256_file(path),
        "outer": parse_outer_header(raw),
        "database": inspect_database(raw, table_names, field_names, field_meta),
    }


def summarize_cross_save(saves: list[dict[str, Any]]) -> dict[str, Any]:
    table_occurrences: dict[str, list[dict[str, Any]]] = {}
    for save in saves:
        db = save.get("database", {})
        for table in db.get("appearance_candidate_tables", []):
            key = f"{table.get('short')}|{table.get('name')}"
            table_occurrences.setdefault(key, []).append({
                "save": save["name"],
                "record_size": table.get("record_size"),
                "valid_records": table.get("valid_records"),
                "appearance_fields": [
                    field.get("name") for field in table.get("appearance_fields", [])
                ],
            })

    return {
        "appearance_tables_seen": [
            {
                "table": key,
                "save_count": len(items),
                "occurrences": items,
            }
            for key, items in sorted(
                table_occurrences.items(),
                key=lambda pair: (-len(pair[1]), pair[0]),
            )
        ],
    }


def main() -> int:
    repo = find_repo()
    settings = Path(os.environ.get("LOCALAPPDATA", "")) / "EA SPORTS FC 26" / "settings"
    if not settings.exists():
        raise RuntimeError(f"Dossier introuvable : {settings}")

    saves = sorted(
        [path for path in settings.glob("CmPlr*") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
    )
    if not saves:
        raise RuntimeError("Aucune sauvegarde CmPlr trouvée.")

    table_names, field_names, field_meta, metadata_source = load_metadata(repo)
    print(f"Métadonnées tables : {len(table_names)}")
    print(f"Métadonnées champs : {len(field_names)}")

    reports = []
    for path in saves:
        print(f"Analyse T3DB : {path.name}")
        reports.append(inspect_save(path, table_names, field_names, field_meta))

    report = {
        "schema": SCHEMA,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety": {
            "read_only": True,
            "game_process_opened": False,
            "game_files_modified": False,
            "save_files_modified": False,
            "anti_cheat_touched": False,
            "raw_save_contents_uploaded": False,
            "personal_names_exported": False,
        },
        "repository": {"remote": git_remote(repo)},
        "metadata": {
            "source": metadata_source,
            "tables": len(table_names),
            "fields": len(field_names),
        },
        "career_player_saves": reports,
        "cross_save": summarize_cross_save(reports),
    }

    diagnostics = repo / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    output = diagnostics / "career_db_probe.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    found = sum(1 for save in reports if save.get("database", {}).get("found"))
    candidate_count = sum(
        len(save.get("database", {}).get("appearance_candidate_tables", []))
        for save in reports
    )

    print()
    print("============================================================")
    print(" FC26 CAREER DB PROBE TERMINE")
    print("============================================================")
    print(f"Bases T3DB trouvées : {found}/{len(reports)}")
    print(f"Tables apparence candidates : {candidate_count}")
    print(f"Rapport : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
