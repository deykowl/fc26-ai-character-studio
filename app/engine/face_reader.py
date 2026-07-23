from __future__ import annotations

from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def _load_image(payload: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(payload))
        image = ImageOps.exif_transpose(image).convert("RGB")
    except Exception as exc:
        raise ValueError("Image illisible.") from exc
    maximum = 1900
    if max(image.size) > maximum:
        image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
    return image


def _variants(image: Image.Image) -> list[tuple[str, np.ndarray]]:
    variants: list[tuple[str, Image.Image]] = [("original", image)]
    variants.append(("contraste", ImageEnhance.Contrast(image).enhance(1.28)))
    variants.append(("netteté", image.filter(ImageFilter.UnsharpMask(radius=2.0, percent=165, threshold=2))))
    gray = ImageOps.grayscale(image)
    variants.append(("niveaux_de_gris", Image.merge("RGB", (gray, gray, gray))))
    if min(image.size) < 700:
        scale = min(2.0, 1200 / max(min(image.size), 1))
        variants.append(("agrandie", image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)))
    return [(name, np.asarray(value)) for name, value in variants]


def _distance(points: list[list[float]], a: int, b: int) -> float:
    pa = np.asarray(points[a][:2], dtype=np.float64)
    pb = np.asarray(points[b][:2], dtype=np.float64)
    return float(np.linalg.norm(pa - pb))


def read_faces(images: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    try:
        import mediapipe as mp
    except ImportError as exc:
        raise RuntimeError("MediaPipe n’est pas installé. Relance install_windows.bat.") from exc

    views: list[dict[str, Any]] = []
    errors: list[str] = []
    with mp.solutions.face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.48,
    ) as detector:
        for filename, payload in images:
            image = _load_image(payload)
            selected = None
            selected_variant = ""
            for variant_name, rgb in _variants(image):
                result = detector.process(rgb)
                if result.multi_face_landmarks:
                    selected = result.multi_face_landmarks[0].landmark
                    selected_variant = variant_name
                    break
            if selected is None:
                errors.append(f"{filename}: aucun visage détecté")
                continue

            points = [[float(p.x), float(p.y), float(p.z)] for p in selected]
            xs = np.asarray([p[0] for p in points])
            ys = np.asarray([p[1] for p in points])
            face_width = float(xs.max() - xs.min())
            face_height = float(ys.max() - ys.min())
            left = np.asarray(points[234][:2])
            right = np.asarray(points[454][:2])
            nose = np.asarray(points[1][:2])
            midpoint = (left + right) * 0.5
            yaw_hint = float((nose[0] - midpoint[0]) / max(abs(right[0] - left[0]), 1e-6))
            if yaw_hint < -0.12:
                angle = "trois-quarts / profil gauche"
            elif yaw_hint > 0.12:
                angle = "trois-quarts / profil droit"
            else:
                angle = "face"

            interocular = max(_distance(points, 33, 263), 1e-6)
            jaw_open_ratio = _distance(points, 13, 14) / interocular
            blink_left = _distance(points, 159, 145) / max(_distance(points, 33, 133), 1e-6)
            blink_right = _distance(points, 386, 374) / max(_distance(points, 362, 263), 1e-6)
            expression = {
                "jawOpen": float(np.clip((jaw_open_ratio - 0.025) / 0.15, 0.0, 1.0)),
                "eyeBlinkLeft": float(np.clip((0.20 - blink_left) / 0.15, 0.0, 1.0)),
                "eyeBlinkRight": float(np.clip((0.20 - blink_right) / 0.15, 0.0, 1.0)),
            }
            views.append({
                "filename": filename,
                "landmarks": points,
                "blendshapes": expression,
                "angle": angle,
                "yaw_hint": yaw_hint,
                "face_width": face_width,
                "face_height": face_height,
                "image_width": int(image.width),
                "image_height": int(image.height),
                "detection_variant": selected_variant,
            })

    if not views:
        detail = "; ".join(errors) if errors else "aucun visage détecté"
        raise ValueError(detail)
    return views
