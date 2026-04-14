from __future__ import annotations

from pathlib import Path


def cv2_imread(path: Path, flags: int = 1):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("OpenCV y numpy son requeridos para cargar imagenes.") from exc

    if not path.exists():
        return None
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, flags)


def cv2_imwrite(path: Path, image) -> bool:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para guardar imagenes.") from exc

    extension = path.suffix.lower() or ".png"
    success, encoded = cv2.imencode(extension, image)
    if not success:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded.tofile(str(path))
    except OSError:
        return False
    return True
