from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import json
import math

import numpy as np
from scipy.optimize import lsq_linear

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "app" / "static" / "assets"

MP2DLIB = [
    [127], [234], [93], [132, 58], [58, 172], [136], [150], [176], [152],
    [400], [379], [365], [397, 288], [361], [323], [454], [356],
    [70], [63], [105], [66], [107], [336], [296], [334], [293], [300],
    [168, 6], [197, 195], [5], [4], [75], [97], [2], [326], [305],
    [33], [160], [158], [133], [153], [144],
    [362], [385], [387], [263], [373], [380],
    [61], [39], [37], [0], [267], [269], [291],
    [321], [314], [17], [84], [91],
    [78], [82], [13], [312], [308],
    [317], [14], [87],
]

LANDMARK_LABELS = [
    *[f"jaw_{i}" for i in range(17)],
    *[f"brow_r_{i}" for i in range(5)],
    *[f"brow_l_{i}" for i in range(5)],
    *[f"nose_{i}" for i in range(9)],
    *[f"eye_r_{i}" for i in range(6)],
    *[f"eye_l_{i}" for i in range(6)],
    *[f"mouth_{i}" for i in range(20)],
]


def _classify_axis(meta: dict[str, Any]) -> str:
    text = f"{meta['control_id']} {meta['control']}".upper()
    if any(t in text for t in ("EYE", "EYELID", "ORBIT", "BROW", "EPICANTH", "PERIORBIT")):
        return "eyes_brows"
    if any(t in text for t in ("NOSE", "NASAL", "COLUMELLA", "SUPRATIP")):
        return "nose"
    if any(t in text for t in ("LIP", "MOUTH", "ORAL", "PHILTR", "TEETH", "INCISOR", "CANINE", "CUSPID", "MOLAR")):
        return "mouth_teeth"
    if any(t in text for t in ("EAR", "HELIX", "TRAGUS", "LOBE")):
        return "ears"
    if any(t in text for t in ("JAW", "MANDIBLE", "CHIN", "MENTAL", "CHEEK", "ZYGOMAX", "BUCCAL")):
        return "jaw_cheeks"
    if any(t in text for t in ("SKULL", "CORONAL", "FOREHEAD", "TEMPLE", "NECK", "LARYN", "ADAMS")):
        return "head_neck"
    if meta["layer"] in {"Flesh", "Fat", "Secondary Form"}:
        return "soft_tissue"
    return "other"


@dataclass
class Runtime:
    vertices: np.ndarray
    faces: np.ndarray
    offsets: np.ndarray
    indices: np.ndarray
    deltas: np.ndarray
    controls: list[dict[str, Any]]
    anchor_ids: np.ndarray
    axis_meta: list[dict[str, Any]]
    axis_morph_indices: np.ndarray
    anchor_basis: np.ndarray
    region_indices: dict[str, np.ndarray]

    @classmethod
    def load(cls) -> "Runtime":
        z = np.load(ENGINE_DIR / "fc26_exact_runtime_lod0.npz")
        controls = json.loads((ASSET_DIR / "shape_controls.json").read_text(encoding="utf-8"))
        anchor_ids = np.asarray(
            json.loads((ASSET_DIR / "fc68_anchor_map.json").read_text(encoding="utf-8"))["fc_vertex_indices"],
            dtype=np.int32,
        )
        axis_meta: list[dict[str, Any]] = []
        for control_index, control in enumerate(controls):
            for axis_index, axis in enumerate(control["axes"]):
                item = {
                    **axis,
                    "control_index": control_index,
                    "axis_index": axis_index,
                    "control_id": control["id"],
                    "control": control["control"],
                    "layer": control["layer"],
                }
                item["region"] = _classify_axis(item)
                axis_meta.append(item)

        morph_indices = np.asarray([item["morph_index"] for item in axis_meta], dtype=np.int32)
        basis = np.zeros((68, 3, len(axis_meta)), dtype=np.float64)
        lookup = {int(vertex_id): row for row, vertex_id in enumerate(anchor_ids)}
        offsets = z["offsets"]
        indices = z["indices"]
        deltas = z["deltas"]
        for axis_column, morph_index in enumerate(morph_indices):
            start = int(offsets[morph_index])
            end = int(offsets[morph_index + 1])
            for vertex_id, delta in zip(indices[start:end], deltas[start:end]):
                row = lookup.get(int(vertex_id))
                if row is not None:
                    basis[row, :, axis_column] += delta

        regions: dict[str, list[int]] = {}
        for index, meta in enumerate(axis_meta):
            regions.setdefault(meta["region"], []).append(index)
        region_indices = {key: np.asarray(value, dtype=np.int32) for key, value in regions.items()}

        return cls(
            vertices=z["vertices"].astype(np.float64),
            faces=z["faces"].astype(np.uint32),
            offsets=offsets,
            indices=indices,
            deltas=deltas.astype(np.float64),
            controls=controls,
            anchor_ids=anchor_ids,
            axis_meta=axis_meta,
            axis_morph_indices=morph_indices,
            anchor_basis=basis,
            region_indices=region_indices,
        )

    def dense_delta(self, theta: np.ndarray) -> np.ndarray:
        output = np.zeros_like(self.vertices)
        for coefficient, morph_index in zip(theta, self.axis_morph_indices):
            if abs(float(coefficient)) < 1e-10:
                continue
            start = int(self.offsets[morph_index])
            end = int(self.offsets[morph_index + 1])
            output[self.indices[start:end]] += self.deltas[start:end] * float(coefficient)
        return output

    def recipe(self, theta: np.ndarray) -> dict[str, Any]:
        controls: list[dict[str, Any]] = []
        cursor = 0
        for control in self.controls:
            axes = []
            for axis in control["axes"]:
                value = float(theta[cursor])
                axes.append({
                    "axis": axis["axis"],
                    "slot": axis["slot"],
                    "morph": axis["morph"],
                    "morph_index": axis["morph_index"],
                    "value": value,
                    "percent": round(value * 100.0, 2),
                })
                cursor += 1
            controls.append({
                "id": control["id"],
                "control": control["control"],
                "display_name": control.get("display_name", control["control"]),
                "category": control.get("category", "Head"),
                "region": control.get("region", "other"),
                "layer": control["layer"],
                "axes": axes,
                "strength": max((abs(axis["value"]) for axis in axes), default=0.0),
            })
        return {
            "schema": "fc26-ai-character-studio/exact-recipe-1.0",
            "axis_count": len(theta),
            "control_count": len(controls),
            "controls": controls,
        }


RUNTIME = Runtime.load()


def mp_to_dlib68(landmarks: list[list[float]]) -> np.ndarray:
    array = np.asarray(landmarks, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] < 468 or array.shape[1] < 3:
        raise ValueError("Chaque vue doit contenir au moins 468 points [x,y,z].")
    output = np.empty((68, 3), dtype=np.float64)
    for row, source_indices in enumerate(MP2DLIB):
        output[row] = array[source_indices, :3].mean(axis=0)
    # MediaPipe: x droite, y vers le bas, z vers la caméra avec convention opposée au renderer.
    output[:, 1] *= -1.0
    output[:, 2] *= -1.0
    return output


def _similarity_fit(source: np.ndarray, target: np.ndarray, weights: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    normalized = weights / max(float(weights.sum()), 1e-12)
    source_center = (source * normalized[:, None]).sum(axis=0)
    target_center = (target * normalized[:, None]).sum(axis=0)
    source_zero = source - source_center
    target_zero = target - target_center
    covariance = (source_zero * normalized[:, None]).T @ target_zero
    u, singular, vt = np.linalg.svd(covariance)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    denominator = float((normalized[:, None] * source_zero * source_zero).sum())
    scale = float(singular.sum() / max(denominator, 1e-12))
    translation = target_center - scale * (rotation @ source_center)
    return scale, rotation, translation


def _landmark_weights(
    blendshapes: dict[str, float] | None = None,
    yaw_hint: float = 0.0,
) -> np.ndarray:
    weights = np.ones(68, dtype=np.float64)
    weights[17:27] = 0.78
    weights[27:48] = 1.22
    weights[48:68] = 1.16
    if blendshapes:
        expression = max(
            float(blendshapes.get("jawOpen", 0.0)),
            float(blendshapes.get("mouthSmileLeft", 0.0)),
            float(blendshapes.get("mouthSmileRight", 0.0)),
            float(blendshapes.get("mouthFrownLeft", 0.0)),
            float(blendshapes.get("mouthFrownRight", 0.0)),
        )
        if expression > 0.22:
            weights[48:68] *= max(0.32, 1.0 - expression)
        blink = max(
            float(blendshapes.get("eyeBlinkLeft", 0.0)),
            float(blendshapes.get("eyeBlinkRight", 0.0)),
        )
        if blink > 0.28:
            weights[36:48] *= max(0.35, 1.0 - blink)
    turn = min(abs(float(yaw_hint)), 0.55) / 0.55
    if turn > 0.05:
        # Les points centraux et la profondeur du nez restent fiables ; les deux
        # contours sont moins sûrs quand une moitié du visage est cachée.
        weights[0:17] *= 1.0 - 0.38 * turn
        weights[17:27] *= 1.0 - 0.22 * turn
        weights[36:48] *= 1.0 - 0.18 * turn
        weights[27:36] *= 1.0 + 0.20 * turn
    return weights


def _normalized_rmse(fitted: np.ndarray, target: np.ndarray) -> float:
    eye_left = target[36:42].mean(axis=0)
    eye_right = target[42:48].mean(axis=0)
    interocular = max(float(np.linalg.norm(eye_left - eye_right)), 1e-9)
    rmse = float(np.sqrt(np.mean(np.sum((fitted - target) ** 2, axis=1))))
    return rmse / interocular


def _score(normalized_rmse: float) -> float:
    return float(max(0.0, min(100.0, 100.0 * math.exp(-normalized_rmse * 15.0))))


def _solve_stage(
    theta: np.ndarray,
    targets: list[np.ndarray],
    view_weights: list[np.ndarray],
    active: np.ndarray,
    iterations: int = 4,
    ridge: float = 0.20,
    sparse: float = 0.048,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    p0 = RUNTIME.vertices[RUNTIME.anchor_ids]
    basis = RUNTIME.anchor_basis
    effect = np.linalg.norm(basis.reshape(-1, basis.shape[2]), axis=0)
    median_effect = float(np.median(effect[effect > 1e-10]))
    penalty = ridge / (effect / max(median_effect, 1e-12) + 0.22)
    penalty[effect < 1e-10] = 1e5
    history: list[dict[str, Any]] = []

    for iteration in range(iterations):
        current = p0 + np.einsum("ijk,k->ij", basis, theta)
        matrix_parts = []
        vector_parts = []
        transforms = []
        for target, landmark_weights in zip(targets, view_weights):
            scale, rotation, translation = _similarity_fit(current, target, landmark_weights)
            transforms.append((scale, rotation, translation))
            frozen = theta.copy()
            frozen[active] = 0.0
            frozen_shape = p0 + np.einsum("ijk,k->ij", basis, frozen)
            base_transformed = (scale * (rotation @ frozen_shape.T)).T + translation
            transformed_basis = np.einsum("ab,ibk->iak", rotation, basis) * scale
            weights_xyz = np.repeat(np.sqrt(landmark_weights), 3)
            matrix_parts.append(transformed_basis.reshape(-1, basis.shape[2])[:, active] * weights_xyz[:, None] * 1000.0)
            vector_parts.append((target - base_transformed).reshape(-1) * weights_xyz * 1000.0)

        matrix = np.vstack(matrix_parts)
        vector = np.concatenate(vector_parts)
        current_active = theta[active]
        l1_weight = 1.0 / (np.abs(current_active) + 0.045)
        diagonal = np.sqrt(penalty[active] ** 2 + (sparse * l1_weight) ** 2)
        matrix_augmented = np.vstack([matrix, np.diag(diagonal)])
        vector_augmented = np.concatenate([vector, np.zeros(len(active))])
        result = lsq_linear(
            matrix_augmented,
            vector_augmented,
            bounds=(-1.0, 1.0),
            lsmr_tol="auto",
            max_iter=350,
        )
        theta[active] = result.x

        normalized = []
        current = p0 + np.einsum("ijk,k->ij", basis, theta)
        for target, landmark_weights in zip(targets, view_weights):
            scale, rotation, translation = _similarity_fit(current, target, landmark_weights)
            fitted = (scale * (rotation @ current.T)).T + translation
            normalized.append(_normalized_rmse(fitted, target))
        mean_error = float(np.mean(normalized))
        history.append({
            "iteration": iteration + 1,
            "normalized_rmse": mean_error,
            "score": _score(mean_error),
            "theta": theta.copy(),
        })
    return theta, history


def fit_views(
    views: list[dict[str, Any]],
    progress: Callable[[dict[str, Any]], None] | None = None,
    initial_theta: list[float] | np.ndarray | None = None,
    locked_regions: set[str] | None = None,
    target_regions: set[str] | None = None,
) -> dict[str, Any]:
    if not views:
        raise ValueError("Aucune vue reçue.")
    targets = [mp_to_dlib68(view["landmarks"]) for view in views]
    weights = [
        _landmark_weights(view.get("blendshapes"), float(view.get("yaw_hint", 0.0)))
        for view in views
    ]
    if initial_theta is None:
        theta = np.zeros(len(RUNTIME.axis_meta), dtype=np.float64)
    else:
        theta = np.asarray(initial_theta, dtype=np.float64).copy()
        if theta.shape != (len(RUNTIME.axis_meta),):
            raise ValueError("Le vecteur de paramètres doit contenir exactement 618 valeurs.")
        theta = np.clip(theta, -1.0, 1.0)
    locked_regions = locked_regions or set()

    stages = [
        ("Structure du crâne", {"head_neck", "jaw_cheeks", "ears"}),
        ("Yeux et sourcils", {"head_neck", "jaw_cheeks", "ears", "eyes_brows"}),
        ("Nez", {"head_neck", "jaw_cheeks", "ears", "eyes_brows", "nose"}),
        ("Bouche, menton et dents", {"head_neck", "jaw_cheeks", "ears", "eyes_brows", "nose", "mouth_teeth"}),
        ("Flesh, Fat et formes secondaires", {"head_neck", "jaw_cheeks", "ears", "eyes_brows", "nose", "mouth_teeth", "soft_tissue", "other"}),
    ]

    snapshots: list[dict[str, Any]] = []
    for stage_index, (stage_name, regions) in enumerate(stages):
        enabled_regions = regions - locked_regions
        if target_regions is not None:
            enabled_regions &= target_regions
        active = np.asarray(
            [index for index, meta in enumerate(RUNTIME.axis_meta) if meta["region"] in enabled_regions],
            dtype=np.int32,
        )
        if active.size == 0:
            continue
        theta, stage_history = _solve_stage(theta, targets, weights, active)
        for local_index, item in enumerate(stage_history):
            sparse_theta = [
                [int(index), round(float(value), 6)]
                for index, value in enumerate(item["theta"])
                if abs(float(value)) >= 0.0001
            ]
            snapshot = {
                "stage": stage_name,
                "stage_index": stage_index,
                "iteration": local_index + 1,
                "score": round(item["score"], 3),
                "normalized_rmse": item["normalized_rmse"],
                "theta": sparse_theta,
            }
            snapshots.append(snapshot)
            if progress is not None:
                progress(snapshot)

    recipe = RUNTIME.recipe(theta)
    recipe["score"] = snapshots[-1]["score"] if snapshots else 0.0
    recipe["views_used"] = len(views)
    recipe["axis_values"] = theta.tolist()
    return {"recipe": recipe, "snapshots": snapshots}
