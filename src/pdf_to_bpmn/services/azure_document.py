from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from pdf_to_bpmn.config import Settings


@dataclass
class OcrLine:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float | None = None

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


class AzureDocumentIntelligenceClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return self.settings.has_document_intelligence

    def read_lines(self, image_path: Path) -> list[OcrLine]:
        if not self.is_configured():
            return []

        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "Falta requests para consumir Azure Document Intelligence."
            ) from exc

        analyze_url = (
            self.settings.azure_doc_endpoint.rstrip("/")
            + f"/documentintelligence/documentModels/{self.settings.azure_doc_model}:analyze"
            + f"?api-version={self.settings.azure_doc_api_version}"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.settings.azure_doc_key or "",
            "Content-Type": "application/octet-stream",
        }

        response = requests.post(
            analyze_url,
            headers=headers,
            data=image_path.read_bytes(),
            timeout=60,
        )
        response.raise_for_status()
        operation_location = response.headers.get("operation-location")
        if not operation_location:
            raise RuntimeError("Azure Document Intelligence no devolvio operation-location.")

        for _ in range(60):
            poll_response = requests.get(
                operation_location,
                headers={"Ocp-Apim-Subscription-Key": self.settings.azure_doc_key or ""},
                timeout=60,
            )
            poll_response.raise_for_status()
            payload = poll_response.json()
            status = payload.get("status", "").lower()
            if status == "succeeded":
                return self._parse_lines(payload)
            if status == "failed":
                raise RuntimeError(f"Azure Document Intelligence fallo: {payload}")
            time.sleep(2.0)

        raise TimeoutError("Azure Document Intelligence no completo la lectura a tiempo.")

    def _parse_lines(self, payload: dict) -> list[OcrLine]:
        analyze_result = payload.get("analyzeResult", {})
        lines: list[OcrLine] = []
        for page in analyze_result.get("pages", []):
            for line in page.get("lines", []):
                polygon = line.get("polygon") or []
                if polygon:
                    xs = polygon[0::2]
                    ys = polygon[1::2]
                    x = min(xs)
                    y = min(ys)
                    width = max(xs) - x
                    height = max(ys) - y
                else:
                    x = float(line.get("x", 0))
                    y = float(line.get("y", 0))
                    width = float(line.get("width", 0))
                    height = float(line.get("height", 0))
                lines.append(
                    OcrLine(
                        text=line.get("content", "").strip(),
                        x=float(x),
                        y=float(y),
                        width=float(width),
                        height=float(height),
                        confidence=line.get("confidence"),
                    )
                )
        return lines
