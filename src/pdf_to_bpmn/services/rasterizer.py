from __future__ import annotations

from pathlib import Path


class SinglePagePdfRasterizer:
    def __init__(self, dpi: int = 300) -> None:
        self.dpi = dpi

    def rasterize(self, input_path: Path, output_path: Path) -> Path:
        suffix = input_path.suffix.lower()
        if suffix == ".pdf":
            return self._rasterize_pdf(input_path, output_path)
        if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
            return self._normalize_image(input_path, output_path)
        raise ValueError(f"Formato de entrada no soportado: {input_path.suffix}")

    def _rasterize_pdf(self, pdf_path: Path, output_path: Path) -> Path:
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PyMuPDF no esta instalado. Instala dependencias antes de rasterizar."
            ) from exc

        with fitz.open(pdf_path) as document:
            if document.page_count != 1:
                raise ValueError(
                    f"Se esperaba un PDF de una sola pagina y se recibieron {document.page_count}."
                )
            page = document.load_page(0)
            zoom = self.dpi / 72.0
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap.save(output_path)
        return output_path

    def _normalize_image(self, image_path: Path, output_path: Path) -> Path:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "OpenCV no esta instalado. Instala dependencias antes de procesar imagenes."
            ) from exc

        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"No se pudo abrir la imagen de entrada: {image_path}")
        if not cv2.imwrite(str(output_path), image):
            raise RuntimeError(f"No se pudo normalizar la imagen de entrada: {image_path}")
        return output_path
