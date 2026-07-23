from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import math
import os
import re
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Iterable

REPORT_SCHEMA = "fc26-ai-character-studio/career-save-probe-1.1"
BLOCK_SIZE = 64 * 1024
SMALL_BLOCK_SIZE = 4 * 1024
MAX_DIFF_RANGES = 300
MAX_DECOMPRESS_CANDIDATES = 120
MAX_DECOMPRESSED_BYTES = 4 * 1024 * 1024

TECHNICAL_TERMS = {
    "career", "player", "cmplr", "manager", "vpro", "virtualpro", "appearance",
    "head", "face", "cranium", "morph", "hair", "beard", "brow", "eyebrow",
    "skin", "tone", "complexion", "gender", "female", "male", "body", "bodytype",
    "height", "weight", "eye", "iris", "nose", "jaw", "chin", "mouth", "lip",
    "cheek", "ear", "forehead", "neck", "shoulder", "torso", "arm", "leg",
    "position", "archetype", "firstname", "lastname", "createplayer",
}
SENSITIVE_TERMS = {"email", "token", "password", "account", "userid", "persona", "nucleus"}

MAGICS = {
    "sqlite": b"SQLite format 3\x00",
    "gzip": b"\x1f\x8b",
    "zip": b"PK\x03\x04",
    "bzip2": b"BZh",
    "xz": b"\xfd7zXZ\x00",
    "lz4_frame": b"\x04\x22\x4d\x18",
    "zstd": b"\x28\xb5\x2f\xfd",
}
ZLIB_HEADERS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for value in data:
        counts[value] += 1
    length = len(data)
    return -sum((count / length) * math.log2(count / length) for count in counts if count)


def sample_entropy(data: bytes) -> dict[str, float]:
    if not data:
        return {}
    width = min(BLOCK_SIZE, len(data))
    starts = sorted(set([
        0,
        max(0, len(data) // 4 - width // 2),
        max(0, len(data) // 2 - width // 2),
        max(0, (len(data) * 3) // 4 - width // 2),
        max(0, len(data) - width),
    ]))
    labels = ["start", "quarter", "middle", "three_quarters", "end"]
    values = {}
    for label, start in zip(labels, starts):
        values[label] = round(entropy(data[start:start + width]), 4)
    values["mean"] = round(sum(values.values()) / max(len(values), 1), 4)
    return values


def sanitize_string(value: str) -> str | None:
    text = value.strip().replace("\x00", "")
    if not text or len(text) > 120:
        return None
    lowered = text.lower()
    if any(term in lowered for term in SENSITIVE_TERMS):
        return None
    if not any(term in lowered for term in TECHNICAL_TERMS):
        return None
    # Redact long numeric identifiers while preserving technical labels and small values.
    text = re.sub(r"\d{5,}", lambda m: "#" * min(len(m.group(0)), 12), text)
    return text


def technical_strings(data: bytes, limit: int = 160) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    ascii_pattern = re.compile(rb"[\x20-\x7e]{4,120}")
    utf16_pattern = re.compile(rb"(?:[\x20-\x7e]\x00){4,120}")

    for match in ascii_pattern.finditer(data):
        value = sanitize_string(match.group().decode("ascii", errors="ignore"))
        if value and value not in seen:
            seen.add(value)
            found.append(value)
            if len(found) >= limit:
                return found

    for match in utf16_pattern.finditer(data):
        try:
            decoded = match.group().decode("utf-16le", errors="ignore")
        except Exception:
            continue
        value = sanitize_string(decoded)
        if value and value not in seen:
            seen.add(value)
            found.append(value)
            if len(found) >= limit:
                return found

    return found


def find_offsets(data: bytes, needle: bytes, max_hits: int = 40) -> list[int]:
    hits = []
    start = 0
    while len(hits) < max_hits:
        index = data.find(needle, start)
        if index < 0:
            break
        hits.append(index)
        start = index + 1
    return hits


def detect_signatures(data: bytes) -> dict[str, list[int]]:
    result = {name: find_offsets(data, magic) for name, magic in MAGICS.items()}
    zlib_hits = []
    for header in ZLIB_HEADERS:
        zlib_hits.extend(find_offsets(data, header, 50))
    result["zlib"] = sorted(set(zlib_hits))[:80]
    return {name: offsets for name, offsets in result.items() if offsets}


def limited_output(raw: bytes) -> bytes:
    return raw[:MAX_DECOMPRESSED_BYTES]


def try_decompression(data: bytes, signatures: dict[str, list[int]]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []

    candidates: list[tuple[str, int]] = []
    for kind in ("gzip", "bzip2", "xz", "zlib"):
        for offset in signatures.get(kind, []):
            candidates.append((kind, offset))
    candidates = candidates[:MAX_DECOMPRESS_CANDIDATES]

    for kind, offset in candidates:
        try:
            chunk = data[offset:]
            if kind == "gzip":
                output = gzip.decompress(chunk)
            elif kind == "bzip2":
                output = bz2.decompress(chunk)
            elif kind == "xz":
                output = lzma.decompress(chunk)
            else:
                obj = zlib.decompressobj()
                output = obj.decompress(chunk, MAX_DECOMPRESSED_BYTES)
            if not output:
                continue
            output = limited_output(output)
            attempts.append({
                "kind": kind,
                "offset": offset,
                "output_bytes_sampled": len(output),
                "output_entropy": round(entropy(output), 4),
                "technical_strings": technical_strings(output, 80),
                "sha256_sample": hashlib.sha256(output).hexdigest(),
            })
            if len(attempts) >= 12:
                break
        except Exception:
            continue
    return attempts


def summarize_ranges(flags: bytearray, max_ranges: int = MAX_DIFF_RANGES) -> list[dict[str, int]]:
    ranges = []
    start = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            ranges.append({"start": start, "end": i, "length": i - start})
            start = None
            if len(ranges) >= max_ranges:
                break
    if start is not None and len(ranges) < max_ranges:
        ranges.append({"start": start, "end": len(flags), "length": len(flags) - start})
    return ranges


def merge_block_indices(indices: list[int], block_size: int) -> list[dict[str, int]]:
    if not indices:
        return []
    ranges = []
    start = prev = indices[0]
    for value in indices[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append({
            "start_block": start,
            "end_block": prev,
            "start_offset": start * block_size,
            "end_offset": (prev + 1) * block_size,
        })
        start = prev = value
    ranges.append({
        "start_block": start,
        "end_block": prev,
        "start_offset": start * block_size,
        "end_offset": (prev + 1) * block_size,
    })
    return ranges


def compare_files(path_a: Path, path_b: Path) -> dict[str, Any]:
    data_a = path_a.read_bytes()
    data_b = path_b.read_bytes()
    common = min(len(data_a), len(data_b))

    equal_bytes = 0
    diff_flags = bytearray(common)
    changed_small_blocks = set()
    changed_large_blocks = set()

    for i in range(common):
        if data_a[i] == data_b[i]:
            equal_bytes += 1
        else:
            diff_flags[i] = 1
            changed_small_blocks.add(i // SMALL_BLOCK_SIZE)
            changed_large_blocks.add(i // BLOCK_SIZE)

    total = max(len(data_a), len(data_b))
    equal_ratio = equal_bytes / total if total else 1.0
    changed_count = total - equal_bytes

    return {
        "a": path_a.name,
        "b": path_b.name,
        "size_a": len(data_a),
        "size_b": len(data_b),
        "same_size": len(data_a) == len(data_b),
        "equal_bytes": equal_bytes,
        "changed_bytes": changed_count,
        "equal_ratio": round(equal_ratio, 8),
        "classification": (
            "mostly_identical_structured"
            if equal_ratio >= 0.90 else
            "partially_shared"
            if equal_ratio >= 0.20 else
            "mostly_different_possible_compression_or_encryption"
        ),
        "changed_byte_ranges_first": summarize_ranges(diff_flags),
        "changed_4k_block_ranges": merge_block_indices(sorted(changed_small_blocks), SMALL_BLOCK_SIZE),
        "changed_64k_block_ranges": merge_block_indices(sorted(changed_large_blocks), BLOCK_SIZE),
        "range_list_truncated": len(summarize_ranges(diff_flags, MAX_DIFF_RANGES + 1)) > MAX_DIFF_RANGES,
    }


def consensus_blocks(paths: list[Path]) -> dict[str, Any] | None:
    if len(paths) < 2:
        return None
    sizes = {p.stat().st_size for p in paths}
    if len(sizes) != 1:
        return {"available": False, "reason": "files_have_different_sizes"}

    handles = [p.open("rb") for p in paths]
    stable = []
    variable = []
    index = 0
    try:
        while True:
            chunks = [h.read(BLOCK_SIZE) for h in handles]
            if not chunks[0]:
                break
            if all(chunk == chunks[0] for chunk in chunks[1:]):
                stable.append(index)
            else:
                variable.append(index)
            index += 1
    finally:
        for h in handles:
            h.close()

    return {
        "available": True,
        "file_count": len(paths),
        "block_size": BLOCK_SIZE,
        "stable_block_ranges": merge_block_indices(stable, BLOCK_SIZE),
        "variable_block_ranges": merge_block_indices(variable, BLOCK_SIZE),
        "stable_blocks": len(stable),
        "variable_blocks": len(variable),
    }


def inspect_file(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    signatures = detect_signatures(data)
    strings = technical_strings(data)

    mean_entropy = sample_entropy(data).get("mean", 0.0)
    if mean_entropy >= 7.55 and len(strings) < 3:
        likely = "compressed_or_encrypted"
    elif strings or mean_entropy < 7.2:
        likely = "structured_binary_or_container"
    else:
        likely = "unknown_binary"

    return {
        "name": path.name,
        "size": len(data),
        "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
        "sha256": sha256_file(path),
        "header_hex_16": data[:16].hex(),
        "tail_sha256_64k": hashlib.sha256(data[-min(len(data), BLOCK_SIZE):]).hexdigest(),
        "entropy_samples": sample_entropy(data),
        "likely_storage": likely,
        "signatures": signatures,
        "technical_strings": strings,
        "decompression_successes": try_decompression(data, signatures),
    }


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


def main() -> int:
    repo = find_repo()
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    settings = local_appdata / "EA SPORTS FC 26" / "settings"

    if not settings.exists():
        raise RuntimeError(f"Dossier de sauvegardes introuvable : {settings}")

    cmplr = sorted(
        [p for p in settings.glob("CmPlr*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    cmmgr = sorted(
        [p for p in settings.glob("CmMgr*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    related = []
    for pattern in ("ProfileOptions*", "Settings*", "Assets*"):
        related.extend(p for p in settings.glob(pattern) if p.is_file())
    related = sorted(set(related), key=lambda p: p.name.lower())

    print(f"Sauvegardes Carrière Joueur trouvées : {len(cmplr)}")
    print(f"Sauvegardes Manager trouvées : {len(cmmgr)}")
    print("Analyse en lecture seule...")

    inspected_cmplr = [inspect_file(path) for path in cmplr]
    inspected_mgr = [inspect_file(path) for path in cmmgr]
    inspected_related = [inspect_file(path) for path in related[:12]]

    comparisons = []
    for i in range(len(cmplr)):
        for j in range(i + 1, len(cmplr)):
            print(f"Comparaison {cmplr[i].name} ↔ {cmplr[j].name}")
            comparisons.append(compare_files(cmplr[i], cmplr[j]))

    report = {
        "schema": REPORT_SCHEMA,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety": {
            "read_only": True,
            "game_process_opened": False,
            "game_files_modified": False,
            "save_files_modified": False,
            "anti_cheat_touched": False,
            "raw_save_contents_uploaded": False,
        },
        "repository": {
            "remote": git_remote(repo),
        },
        "settings_location": "%LOCALAPPDATA%/EA SPORTS FC 26/settings",
        "career_player_files": inspected_cmplr,
        "career_manager_files": inspected_mgr,
        "related_profile_files": inspected_related,
        "career_player_pairwise_comparisons": comparisons,
        "career_player_consensus": consensus_blocks(cmplr),
        "next_step_hint": (
            "If files are mostly identical, a controlled before/after snapshot can map appearance fields. "
            "If files are mostly different and high entropy, save-level direct editing may require an existing "
            "FC26 save parser or an offline in-game bridge."
        ),
    }

    diagnostics = repo / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    output = diagnostics / "career_save_probe.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("============================================================")
    print(" FC26 CAREER SAVE PROBE TERMINE")
    print("============================================================")
    print(f"Rapport : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
