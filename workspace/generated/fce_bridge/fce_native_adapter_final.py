from __future__ import annotations
from typing import Any, Iterable

LAYERS = ("fat", "flesh", "skeletal")

def build_native_payload(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    payload: dict[str, dict[str, float]] = {layer: {} for layer in LAYERS}
    for entry in entries:
        layer = str(entry["layer"])
        morph = str(entry["native_morph"])
        value = float(entry["value"])
        if layer not in payload:
            raise ValueError(f"Unsupported FCE layer: {layer}")
        if morph in payload[layer]:
            raise ValueError(f"Duplicate logical FCE morph: {layer}::{morph}")
        payload[layer][morph] = value
    return payload

def flatten_for_custom_preset(payload: dict[str, dict[str, float]]) -> dict[str, float]:
    flattened: dict[str, float] = {}
    for layer in LAYERS:
        for morph, value in payload.get(layer, {}).items():
            if morph in flattened:
                raise ValueError(f"Morph name appears in multiple layers: {morph}")
            flattened[morph] = float(value)
    return flattened
