from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from app.engine.fitter import RUNTIME, _landmark_weights, _normalized_rmse, _similarity_fit, _solve_stage



def main() -> None:
    print("\nFC26 AI CHARACTER STUDIO — AUTO-TEST\n")
    binary = ROOT / "app" / "static" / "assets" / "fc26_exact_runtime_lod0.bin"
    data = binary.read_bytes()
    if data[:4] != b"FCM1":
        raise RuntimeError("Signature du renderer invalide")
    counts = struct.unpack_from("<5I", data, 4)
    expected = (3355, 6334, 1092, 224598, 3157)
    if counts != expected:
        raise RuntimeError(f"Comptes renderer incorrects : {counts}")
    controls = json.loads((ROOT / "app" / "static" / "assets" / "shape_controls.json").read_text(encoding="utf-8"))
    axis_count = sum(len(control["axes"]) for control in controls)
    if len(controls) != 238 or axis_count != 618:
        raise RuntimeError("Inventaire Cranium incomplet")
    print("[OK] Renderer exact : 3 355 vertices / 6 334 triangles")
    print("[OK] Banque FBMorph : 1 092 morphs")
    print("[OK] Contrôles : 238 / axes : 618")

    rng = np.random.default_rng(260726)
    effect = np.linalg.norm(RUNTIME.anchor_basis.reshape(-1, len(RUNTIME.axis_meta)), axis=0)
    active = np.argsort(effect)[-28:].astype(np.int32)
    truth = np.zeros(len(RUNTIME.axis_meta), dtype=np.float64)
    truth[active] = rng.uniform(-0.48, 0.48, len(active))
    base = RUNTIME.vertices[RUNTIME.anchor_ids]
    target = base + np.einsum("ijk,k->ij", RUNTIME.anchor_basis, truth)
    angle = 0.20
    c, s = np.cos(angle), np.sin(angle)
    rotation = np.asarray([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    target = (1.25 * (rotation @ target.T)).T + np.asarray([0.31, -0.19, 0.11])
    fitted_theta, _ = _solve_stage(
        np.zeros_like(truth), [target], [_landmark_weights()], active,
        iterations=6, ridge=0.06, sparse=0.01,
    )
    fitted_shape = base + np.einsum("ijk,k->ij", RUNTIME.anchor_basis, fitted_theta)
    scale, fit_rotation, translation = _similarity_fit(fitted_shape, target, _landmark_weights())
    fitted = (scale * (fit_rotation @ fitted_shape.T)).T + translation
    normalized = _normalized_rmse(fitted, target)
    millimeters = float(np.sqrt(np.mean(np.sum((fitted - target) ** 2, axis=1))) * 1000.0 / scale)
    if millimeters >= 1.0:
        raise RuntimeError(f"Précision inverse insuffisante : {millimeters:.3f} mm")
    print(f"[OK] Solveur inverse : {millimeters:.3f} mm sur la tête synthétique")
    print("\nTOUS LES TESTS SONT PASSÉS.\n")


if __name__ == "__main__":
    main()
