from __future__ import annotations

import base64
import json
import math
import re
import unicodedata
import uuid
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import (
    DiagramDocument,
    DiagramEdge,
    DiagramNode,
    EdgeType,
    EVENT_NODE_TYPES,
    IssueSeverity,
    NODE_LABELS,
    NodeType,
    Point,
    ReviewIssue,
    normalize_event_node_size,
    normalize_event_nodes,
)
from pdf_to_bpmn.services.azure_document import AzureDocumentIntelligenceClient, OcrLine
from pdf_to_bpmn.services.image_io import cv2_imread


class AzureFoundryVisionRecognizer:
    def __init__(self, settings: Settings, sketch_mode: bool = False) -> None:
        self.settings = settings
        self.sketch_mode = sketch_mode

    def is_configured(self) -> bool:
        return self.settings.has_foundry_vision

    def propose(self, pdf_path: Path, image_path: Path) -> dict | None:
        if not self.is_configured():
            return None

        width, height = _read_image_dimensions(image_path)
        system_prompt = _bpmn_extraction_prompt(self.sketch_mode)
        user_content = _build_foundry_user_content(pdf_path, image_path, width, height, self.sketch_mode)
        return self._request_proposal(system_prompt, user_content)

    def refine(self, pdf_path: Path, image_path: Path, current_diagram: DiagramDocument) -> dict | None:
        if not self.is_configured():
            return None

        width, height = _read_image_dimensions(image_path)
        system_prompt = _bpmn_refinement_prompt(self.sketch_mode)
        user_content = _build_foundry_user_content(pdf_path, image_path, width, height, self.sketch_mode)
        user_content.append(
            {
                "type": "input_text",
                "text": (
                    "Usa este diagrama BPMN actual como punto de partida, pero mejoralo en todo sentido. "
                    "Corrige geometria, textos, tipos BPMN, pools, lanes, asociaciones, anotaciones y conectores. "
                    "Si detectas algo mejor que la version actual, reemplazalo.\n\n"
                    f"DIAGRAMA_ACTUAL_JSON:\n{current_diagram.to_json()}"
                ),
            }
        )
        return self._request_proposal(system_prompt, user_content)

    def _request_proposal(self, system_prompt: str, user_content: list[dict]) -> dict:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("Falta requests para consumir Azure AI Foundry.") from exc

        url = self.settings.azure_foundry_responses_url or _build_responses_url(self.settings)
        headers = {
            "Content-Type": "application/json",
            "api-key": self.settings.azure_foundry_api_key or "",
        }
        payload = {
            "model": self.settings.azure_foundry_deployment,
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": system_prompt,
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": user_content,
                },
            ],
            "temperature": 0.1,
            "max_output_tokens": 4000,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "bpmn_extraction",
                    "strict": True,
                    "schema": _bpmn_schema(),
                }
            },
        }
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        if response.status_code == 400 and _should_retry_with_chat_completions(response, user_content):
            return self._request_proposal_via_chat_completions(system_prompt, user_content)
        if not response.ok:
            raise RuntimeError(_format_foundry_error(response))
        raw = response.json()
        content = _extract_responses_text(raw)
        return json.loads(content)

    def _request_proposal_via_chat_completions(self, system_prompt: str, user_content: list[dict]) -> dict:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("Falta requests para consumir Azure AI Foundry.") from exc

        responses_url = self.settings.azure_foundry_responses_url or _build_responses_url(self.settings)
        if "/responses" not in responses_url:
            raise RuntimeError("No se pudo derivar endpoint chat/completions desde Azure Foundry.")
        chat_url = responses_url.rsplit("/responses", 1)[0] + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "api-key": self.settings.azure_foundry_api_key or "",
        }
        payload = {
            "model": self.settings.azure_foundry_deployment,
            "messages": _build_chat_completion_messages(system_prompt, user_content),
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "bpmn_extraction",
                    "strict": True,
                    "schema": _bpmn_schema(),
                },
            },
        }
        response = requests.post(chat_url, headers=headers, json=payload, timeout=120)
        if not response.ok:
            raise RuntimeError(_format_foundry_error(response))
        raw = response.json()
        content = _extract_chat_completion_text(raw)
        return json.loads(content)

def _bpmn_extraction_prompt(sketch_mode: bool = False) -> str:
    sketch_hint = (
        " El origen puede ser un boceto a mano alzada o texto manuscrito. "
        "Tolera bordes irregulares, cajas incompletas, flechas torcidas y letras partidas. "
        "No exijas geometria perfecta para reconocer tareas, eventos o gateways. "
        "Prioriza el flujo visual principal aunque el dibujo sea informal."
        if sketch_mode
        else ""
    )
    return (
        "Analiza un diagrama BPMN 2.0 proveniente de un PDF escaneado o fotografiado. "
        "Responde solo con un objeto JSON que cumpla exactamente el esquema solicitado. "
        "Usa coordenadas en pixeles con origen arriba izquierda. "
        "No inventes elementos fuera de la imagen. "
        "Cuando exista ambigüedad, conserva el elemento más probable con confidence baja "
        "y agrega un issue claro para revisión humana. "
        "Tipos de nodo permitidos: "
        "pool, lane, task, user_task, service_task, subprocess, collapsed_subprocess, "
        "start_event, intermediate_event, end_event, boundary_event, exclusive_gateway, "
        "parallel_gateway, inclusive_gateway, event_based_gateway, data_object, data_store, annotation. "
        "Tipos de edge permitidos: sequence_flow, message_flow, association. "
        + sketch_hint
        + _bpmn_visual_reconstruction_rules()
    )


def _bpmn_refinement_prompt(sketch_mode: bool = False) -> str:
    sketch_hint = (
        " El diagrama puede venir de un boceto a mano alzada. "
        "Mejora sin forzar limpieza artificial: conserva la intencion del trazo, el orden del flujo y el texto manuscrito legible. "
        "Cuando una forma sea irregular pero consistente con tarea, evento o gateway, manten la interpretacion mas probable."
        if sketch_mode
        else ""
    )
    return (
        "Analiza y mejora un diagrama BPMN 2.0 extraido desde un PDF. "
        "Recibiras la imagen/PDF original y un JSON con el diagrama actual. "
        "Debes devolver un JSON completo nuevo, no un diff. "
        "Prioriza fidelidad visual respecto al PDF y coherencia BPMN al mismo tiempo. "
        "Mejora nombres, asociaciones a objetos de datos, anotaciones, pool, lanes, subprocesos, "
        "conectores, waypoints, textos OCR, dimensiones y alineacion general. "
        "Cuando el PDF sugiera una mejora frente al diagrama actual, adopta la mejora. "
        "Cuando algo siga ambiguo, mantenlo pero con confidence baja e issue clara. "
        "Tipos de nodo permitidos: "
        "pool, lane, task, user_task, service_task, subprocess, collapsed_subprocess, "
        "start_event, intermediate_event, end_event, boundary_event, exclusive_gateway, "
        "parallel_gateway, inclusive_gateway, event_based_gateway, data_object, data_store, annotation. "
        "Tipos de edge permitidos: sequence_flow, message_flow, association. "
        + sketch_hint
        + _bpmn_visual_reconstruction_rules()
    )


def _bpmn_visual_reconstruction_rules() -> str:
    return (
        " Prioriza fidelidad visual sobre auto-layout generico. "
        "Conserva pool, lane, encabezados, etiquetas laterales, tareas, subprocesos, eventos, objetos de datos y anotaciones visibles. "
        "Usa la imagen rasterizada como verdad geometrica para x, y, width y height. "
        "No aceptes una interpretacion mas limpia si se aleja del grafico original. "
        "Mantén asociaciones entre tareas y objetos de datos cuando se vean lineas punteadas o proximidad documental. "
        "Preserva quiebres principales de conectores y waypoints cuando sean visibles. "
        "No devuelvas nodos con ancho o alto menor a 10 pixeles. "
        "Si algo es ambiguo, conserva el elemento con confidence baja y agrega un issue claro en vez de omitirlo."
    )


def _build_foundry_user_content(
    pdf_path: Path, image_path: Path, width: int, height: int, sketch_mode: bool = False
) -> list[dict]:
    content: list[dict] = []
    if pdf_path.exists() and pdf_path.suffix.lower() == ".pdf":
        pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_file",
                "filename": pdf_path.name,
                "file_data": f"data:application/pdf;base64,{pdf_b64}",
            }
        )
    else:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{image_b64}",
                "detail": "high",
            }
        )
    content.append(
        {
            "type": "input_text",
            "text": (
                "Extrae el BPMN lo mejor posible. "
                f"Usa coordenadas en pixeles sobre una referencia rasterizada de {width}x{height}."
                + (
                    " El documento debe interpretarse como boceto a mano: espera trazos imperfectos y texto manuscrito."
                    if sketch_mode
                    else ""
                )
            ),
        }
    )
    return content


def _format_foundry_error(response) -> str:
    body = response.text.strip()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            code = error.get("code")
            param = error.get("param")
            parts = [str(part) for part in (code, param, message) if part]
            if parts:
                return f"Azure Foundry devolvio {response.status_code}: {' | '.join(parts)}"
    if body:
        body = body.replace("\r", " ").replace("\n", " ")
        return f"Azure Foundry devolvio {response.status_code}: {body[:600]}"
    return f"Azure Foundry devolvio {response.status_code} sin cuerpo utilizable."


def _build_responses_url(settings: Settings) -> str:
    api_version = (settings.azure_foundry_api_version or "v1").strip()
    base = settings.azure_foundry_endpoint.rstrip("/")
    if "/responses" in base:
        return base
    if api_version.lower() == "v1":
        return base + "/openai/v1/responses"
    return base + f"/openai/v1/responses?api-version={api_version}"


def _bpmn_schema() -> dict:
    point_schema = {
        "type": "object",
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    }
    node_type_values = [node_type.value for node_type in NodeType]
    edge_type_values = [edge_type.value for edge_type in EdgeType]
    return {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "enum": node_type_values},
                        "text": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                        "confidence": {"type": "number"},
                        "parent_id": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                    },
                    "required": [
                        "id",
                        "type",
                        "text",
                        "x",
                        "y",
                        "width",
                        "height",
                        "confidence",
                        "parent_id",
                    ],
                    "additionalProperties": False,
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "enum": edge_type_values},
                        "source_id": {"type": "string"},
                        "target_id": {"type": "string"},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                        "waypoints": {
                            "type": "array",
                            "items": point_schema,
                        },
                    },
                    "required": [
                        "id",
                        "type",
                        "source_id",
                        "target_id",
                        "text",
                        "confidence",
                        "waypoints",
                    ],
                    "additionalProperties": False,
                },
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": [severity.value for severity in IssueSeverity],
                        },
                        "message": {"type": "string"},
                        "related_kind": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "related_id": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                    },
                    "required": [
                        "id",
                        "severity",
                        "message",
                        "related_kind",
                        "related_id",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["nodes", "edges", "issues"],
        "additionalProperties": False,
    }


def _extract_responses_text(raw: dict) -> str:
    if isinstance(raw.get("output_text"), str) and raw["output_text"].strip():
        return raw["output_text"]

    outputs = raw.get("output", [])
    for item in outputs:
        if item.get("type") != "message":
            continue
        parts = item.get("content", [])
        text_parts = [
            part.get("text", "")
            for part in parts
            if part.get("type") == "output_text" and part.get("text")
        ]
        if text_parts:
            return "\n".join(text_parts)

    raise RuntimeError(f"Azure Responses API no devolvio output_text utilizable: {raw}")


def _extract_chat_completion_text(raw: dict) -> str:
    choices = raw.get("choices") or []
    if not choices:
        raise RuntimeError(f"Azure chat/completions no devolvio choices: {raw}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    raise RuntimeError(f"Azure chat/completions no devolvio content utilizable: {raw}")


def _should_retry_with_chat_completions(response, user_content: list[dict]) -> bool:
    if not _contains_input_image(user_content):
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return error.get("code") == "invalid_payload"


def _contains_input_image(user_content: list[dict]) -> bool:
    return any(item.get("type") == "input_image" for item in user_content)


def _build_chat_completion_messages(system_prompt: str, user_content: list[dict]) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    converted_content: list[dict] = []
    for item in user_content:
        item_type = item.get("type")
        if item_type == "input_text":
            converted_content.append({"type": "text", "text": item.get("text", "")})
        elif item_type == "input_image":
            converted_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": item.get("image_url", "")},
                }
            )
    if converted_content:
        messages.append({"role": "user", "content": converted_content})
    return messages


class HybridDiagramAnalyzer:
    def __init__(self, settings: Settings, sketch_mode: bool = False) -> None:
        self.settings = settings
        self.sketch_mode = sketch_mode
        self.ocr_client = AzureDocumentIntelligenceClient(settings)
        self.vision_client = AzureFoundryVisionRecognizer(settings, sketch_mode=sketch_mode)

    def analyze(self, pdf_path: Path, image_path: Path) -> DiagramDocument:
        ocr_lines = self.ocr_client.read_lines(image_path)
        if self.sketch_mode:
            ocr_lines = _prepare_sketch_ocr_lines(ocr_lines)

        proposal = None
        if self.vision_client.is_configured():
            try:
                proposal = self.vision_client.propose(pdf_path, image_path)
            except Exception as exc:  # pragma: no cover - runtime dependent
                proposal = {
                    "nodes": [],
                    "edges": [],
                    "issues": [
                        {
                            "id": f"issue-{uuid.uuid4().hex[:8]}",
                            "severity": "warning",
                            "message": f"Azure Foundry no devolvio propuesta valida: {exc}",
                            "related_kind": "diagram",
                            "related_id": None,
                        }
                    ],
                }

        if proposal and proposal.get("nodes"):
            diagram = self._document_from_proposal(pdf_path, image_path, proposal)
        else:
            diagram = self._heuristic_bootstrap(pdf_path, image_path, ocr_lines)
            if proposal:
                self._append_proposal_issues(diagram, proposal)

        self._merge_ocr_text(diagram, ocr_lines)
        self._suppress_header_artifacts(diagram)
        self._suppress_frame_containers(diagram)
        self._merge_data_store_candidates(diagram, image_path, ocr_lines)
        self._merge_collapsed_subprocess_candidates(diagram, image_path)
        self._infer_semantics(diagram, ocr_lines)
        self._sanitize_boundary_events(diagram)
        normalize_event_nodes(diagram.nodes)
        self._prune_and_infer_sequence_flows(diagram)
        self._merge_geometric_association_candidates(diagram)
        self._validate_semantics(diagram)
        diagram.metadata["title"] = _infer_diagram_title(diagram, ocr_lines)
        diagram.metadata["analysis_mode"] = "sketch" if self.sketch_mode else "standard"
        self._promote_review_issues(diagram)
        return diagram

    def refine(self, pdf_path: Path, image_path: Path, current_diagram: DiagramDocument) -> DiagramDocument:
        ocr_lines = self.ocr_client.read_lines(image_path)
        if self.sketch_mode:
            ocr_lines = _prepare_sketch_ocr_lines(ocr_lines)
        proposal = None
        if self.vision_client.is_configured():
            try:
                proposal = self.vision_client.refine(pdf_path, image_path, current_diagram)
            except Exception as exc:  # pragma: no cover - runtime dependent
                proposal = {
                    "nodes": [],
                    "edges": [],
                    "issues": [
                        {
                            "id": f"issue-{uuid.uuid4().hex[:8]}",
                            "severity": "warning",
                            "message": f"Azure Foundry no devolvio refinamiento valido: {exc}",
                            "related_kind": "diagram",
                            "related_id": None,
                        }
                    ],
                }

        if proposal and proposal.get("nodes"):
            diagram = self._document_from_proposal(pdf_path, image_path, proposal)
            diagram.metadata["bootstrap"] = "azure_foundry_refine"
        else:
            diagram = self.analyze(pdf_path, image_path)

        self._merge_ocr_text(diagram, ocr_lines)
        self._suppress_header_artifacts(diagram)
        self._suppress_frame_containers(diagram)
        self._merge_data_store_candidates(diagram, image_path, ocr_lines)
        self._merge_collapsed_subprocess_candidates(diagram, image_path)
        self._infer_semantics(diagram, ocr_lines)
        self._sanitize_boundary_events(diagram)
        normalize_event_nodes(diagram.nodes)
        self._prune_and_infer_sequence_flows(diagram)
        self._merge_geometric_association_candidates(diagram)
        self._validate_semantics(diagram)
        diagram.metadata["title"] = _infer_diagram_title(diagram, ocr_lines)
        diagram.metadata["analysis_mode"] = "sketch" if self.sketch_mode else "standard"
        self._promote_review_issues(diagram)
        return diagram

    def _document_from_proposal(
        self, pdf_path: Path, image_path: Path, proposal: dict
    ) -> DiagramDocument:
        width, height = _read_image_dimensions(image_path)
        nodes = [
            DiagramNode(
                id=item.get("id", f"node-{uuid.uuid4().hex[:8]}"),
                node_type=_coerce_node_type(item.get("type")),
                text=item.get("text", "").strip(),
                x=float(item.get("x", 0)),
                y=float(item.get("y", 0)),
                width=max(float(item.get("width", 10)), 10.0),
                height=max(float(item.get("height", 10)), 10.0),
                confidence=float(item.get("confidence", 0.5)),
                parent_id=item.get("parent_id"),
                metadata=item.get("metadata", {}),
            )
            for item in proposal.get("nodes", [])
        ]
        edges = [
            DiagramEdge(
                id=item.get("id", f"edge-{uuid.uuid4().hex[:8]}"),
                edge_type=_coerce_edge_type(item.get("type")),
                source_id=item.get("source_id", ""),
                target_id=item.get("target_id", ""),
                text=item.get("text", "").strip(),
                confidence=float(item.get("confidence", 0.5)),
                waypoints=[
                    Point(x=float(point["x"]), y=float(point["y"]))
                    for point in item.get("waypoints", [])
                ],
                metadata=item.get("metadata", {}),
            )
            for item in proposal.get("edges", [])
            if item.get("source_id") and item.get("target_id")
        ]
        issues = [
            ReviewIssue(
                id=item.get("id", f"issue-{uuid.uuid4().hex[:8]}"),
                severity=IssueSeverity(item.get("severity", "warning")),
                message=item.get("message", "Sin detalle."),
                related_kind=item.get("related_kind"),
                related_id=item.get("related_id"),
            )
            for item in proposal.get("issues", [])
        ]
        return DiagramDocument(
            source_pdf=pdf_path,
            source_image=image_path,
            image_width=width,
            image_height=height,
            nodes=nodes,
            edges=edges,
            issues=issues,
            metadata={"bootstrap": "azure_foundry"},
        )

    def _heuristic_bootstrap(
        self, pdf_path: Path, image_path: Path, ocr_lines: list[OcrLine]
    ) -> DiagramDocument:
        width, height = _read_image_dimensions(image_path)
        nodes, contours_found = _detect_node_candidates(
            image_path,
            width,
            height,
            sketch_mode=self.sketch_mode,
        )
        edges = _detect_connectors(image_path, nodes, sketch_mode=self.sketch_mode)
        issues: list[ReviewIssue] = []

        if not contours_found:
            issues.append(
                ReviewIssue(
                    id=f"issue-{uuid.uuid4().hex[:8]}",
                    severity=IssueSeverity.ERROR,
                    message="No se detectaron formas BPMN base en el diagrama. Revisa el preprocesamiento o Azure Foundry.",
                    related_kind="diagram",
                )
            )

        if not ocr_lines:
            issues.append(
                ReviewIssue(
                    id=f"issue-{uuid.uuid4().hex[:8]}",
                    severity=IssueSeverity.WARNING,
                    message="No se obtuvo OCR. Configura Azure Document Intelligence para mejorar textos y clasificacion.",
                    related_kind="diagram",
                )
            )

        if not edges:
            issues.append(
                ReviewIssue(
                    id=f"issue-{uuid.uuid4().hex[:8]}",
                    severity=IssueSeverity.WARNING,
                    message="No se detectaron conectores con confianza suficiente. Revisa conexiones manualmente.",
                    related_kind="diagram",
                )
            )

        return DiagramDocument(
            source_pdf=pdf_path,
            source_image=image_path,
            image_width=width,
            image_height=height,
            nodes=nodes,
            edges=edges,
            issues=issues,
            metadata={
                "bootstrap": "heuristic",
                "analysis_mode": "sketch" if self.sketch_mode else "standard",
            },
        )

    def _merge_ocr_text(self, diagram: DiagramDocument, lines: list[OcrLine]) -> None:
        assigned: dict[str, list[OcrLine]] = defaultdict(list)
        for line in lines:
            node = _find_best_container(diagram.nodes, line)
            if node:
                assigned[node.id].append(line)
        for node in diagram.nodes:
            node_lines = list(assigned.get(node.id, []))
            if node.node_type == NodeType.ANNOTATION:
                for line in _collect_annotation_lines(node, lines):
                    if line not in node_lines:
                        node_lines.append(line)
            if not node_lines:
                continue
            cleaned_text, metadata = _extract_node_text_from_ocr(node, node_lines, diagram.image_height)
            if cleaned_text:
                node.text = cleaned_text
            if metadata:
                node.metadata.update(metadata)

    def _promote_review_issues(self, diagram: DiagramDocument) -> None:
        existing_keys = {
            (issue.related_kind, issue.related_id, issue.message)
            for issue in diagram.issues
        }
        for node in diagram.nodes:
            if node.confidence < self.settings.confidence_threshold:
                message = (
                    f"Revisar clasificacion y geometria de '{NODE_LABELS.get(node.node_type, node.node_type.value)}'."
                )
                key = ("node", node.id, message)
                if key not in existing_keys:
                    diagram.issues.append(
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.WARNING,
                            message=message,
                            related_kind="node",
                            related_id=node.id,
                        )
                    )

    def _suppress_header_artifacts(self, diagram: DiagramDocument) -> None:
        top_limit = diagram.image_height * 0.26
        for node in diagram.nodes:
            if node.deleted:
                continue
            if node.center.y > top_limit:
                continue
            normalized = _normalize_text(node.text)
            if _looks_like_document_header_artifact(
                node,
                normalized,
                diagram.image_width,
                diagram.image_height,
            ):
                node.deleted = True

    def _suppress_frame_containers(self, diagram: DiagramDocument) -> None:
        pools = [node for node in diagram.nodes if not node.deleted and node.node_type == NodeType.POOL]
        lanes = [node for node in diagram.nodes if not node.deleted and node.node_type == NodeType.LANE]
        tasks = [node for node in diagram.nodes if not node.deleted and node.node_type == NodeType.TASK]
        if len(pools) == 1:
            pool = pools[0]
            task_texts = {_normalize_text(node.text) for node in tasks if node.text.strip()}
            pool_text = _normalize_text(pool.text)
            if pool_text and pool_text in task_texts:
                pool.deleted = True
        if len(pools) != 1 or len(lanes) != 1:
            return
        pool = pools[0]
        lane = lanes[0]
        lane_text = _normalize_text(lane.text)
        if pool.width < diagram.image_width * 0.9 or lane.width < diagram.image_width * 0.85:
            return
        if lane.height > diagram.image_height * 0.4:
            return
        if not _node_contains_node(pool, lane):
            return
        if lane_text and ("cuentas por cobrar" in lane_text or re.search(r"\b0\d\b", lane_text)):
            lane.deleted = True

    def _merge_missing_from_previous(self, diagram: DiagramDocument, previous: DiagramDocument) -> None:
        existing_keys = {
            (issue.related_kind, issue.related_id, issue.message)
            for issue in diagram.issues
        }
        existing_nodes = {node.id for node in diagram.nodes}
        existing_edges = {edge.id for edge in diagram.edges}
        for node in previous.nodes:
            if node.deleted or node.id in existing_nodes:
                continue
            diagram.nodes.append(deepcopy(node))
        for edge in previous.edges:
            if edge.deleted or edge.id in existing_edges:
                continue
            diagram.edges.append(deepcopy(edge))
        for edge in diagram.edges:
            if edge.confidence < self.settings.confidence_threshold:
                message = "Revisar origen, destino y tipo del flujo."
                key = ("edge", edge.id, message)
                if key not in existing_keys:
                    diagram.issues.append(
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.WARNING,
                            message=message,
                            related_kind="edge",
                            related_id=edge.id,
                        )
                    )

    def _append_proposal_issues(self, diagram: DiagramDocument, proposal: dict) -> None:
        existing_keys = {
            (issue.related_kind, issue.related_id, issue.message)
            for issue in diagram.issues
        }
        for item in proposal.get("issues", []):
            issue = ReviewIssue(
                id=item.get("id", f"issue-{uuid.uuid4().hex[:8]}"),
                severity=IssueSeverity(item.get("severity", "warning")),
                message=item.get("message", "Sin detalle."),
                related_kind=item.get("related_kind"),
                related_id=item.get("related_id"),
            )
            key = (issue.related_kind, issue.related_id, issue.message)
            if key in existing_keys:
                continue
            diagram.issues.append(issue)

    def _merge_data_store_candidates(
        self, diagram: DiagramDocument, image_path: Path, ocr_lines: list[OcrLine]
    ) -> None:
        candidates = _detect_data_store_candidates(image_path, ocr_lines, diagram.image_height)
        if not candidates:
            return

        for candidate in candidates:
            best_match: DiagramNode | None = None
            best_score = 0.0
            for node in diagram.nodes:
                if node.deleted or node.node_type in {NodeType.POOL, NodeType.LANE}:
                    continue
                overlap = _node_overlap_ratio(node, candidate)
                if overlap > best_score:
                    best_score = overlap
                    best_match = node

            if best_match and best_score >= 0.12:
                best_match.node_type = NodeType.DATA_STORE
                best_match.x = candidate.x
                best_match.y = candidate.y
                best_match.width = candidate.width
                best_match.height = candidate.height
                if candidate.text.strip():
                    best_match.text = candidate.text
                best_match.confidence = max(best_match.confidence, candidate.confidence)
                best_match.metadata["inferred_from"] = "data_store_detector"
                continue

            diagram.nodes.append(candidate)

    def _merge_collapsed_subprocess_candidates(self, diagram: DiagramDocument, image_path: Path) -> None:
        for node in diagram.nodes:
            if node.deleted or node.node_type not in {
                NodeType.TASK,
                NodeType.SUBPROCESS,
                NodeType.COLLAPSED_SUBPROCESS,
                NodeType.USER_TASK,
                NodeType.SERVICE_TASK,
            }:
                continue
            if _detect_collapsed_marker(image_path, node):
                node.metadata["plus_marker"] = True
                if node.node_type in {NodeType.TASK, NodeType.SUBPROCESS}:
                    node.node_type = NodeType.COLLAPSED_SUBPROCESS
                    node.confidence = min(max(node.confidence, 0.62) + 0.08, 0.95)

    def _merge_boundary_event_candidates(self, diagram: DiagramDocument, image_path: Path) -> None:
        candidates = _detect_boundary_event_candidates(image_path, diagram.nodes)
        if not candidates:
            return
        if len(candidates) > 8:
            return

        for candidate, attached_to in candidates:
            candidate.metadata["attached_to"] = attached_to.id
            candidate.parent_id = attached_to.parent_id
            best_match = next(
                (
                    node
                    for node in diagram.nodes
                    if not node.deleted
                    and node.node_type in {
                        NodeType.START_EVENT,
                        NodeType.INTERMEDIATE_EVENT,
                        NodeType.END_EVENT,
                        NodeType.BOUNDARY_EVENT,
                    }
                    and _node_overlap_ratio(node, candidate) > 0.45
                ),
                None,
            )
            if best_match:
                best_match.node_type = NodeType.BOUNDARY_EVENT
                best_match.x = candidate.x
                best_match.y = candidate.y
                best_match.width = candidate.width
                best_match.height = candidate.height
                best_match.parent_id = candidate.parent_id
                best_match.metadata["attached_to"] = attached_to.id
                if (candidate.metadata or {}).get("attached_side"):
                    best_match.metadata["attached_side"] = candidate.metadata["attached_side"]
                best_match.metadata["inferred_from"] = "boundary_event_detector"
                best_match.confidence = max(best_match.confidence, candidate.confidence)
                normalize_event_node_size(best_match)
                continue
            diagram.nodes.append(candidate)

    def _sanitize_boundary_events(self, diagram: DiagramDocument) -> None:
        nodes_by_id = {node.id: node for node in diagram.nodes if not node.deleted}
        for node in diagram.nodes:
            if node.deleted or node.node_type != NodeType.BOUNDARY_EVENT:
                continue
            attached_to_id = str((node.metadata or {}).get("attached_to") or "").strip()
            attached_to = nodes_by_id.get(attached_to_id) if attached_to_id else None
            if not attached_to:
                node.node_type = NodeType.INTERMEDIATE_EVENT
                node.metadata.pop("attached_to", None)
                node.metadata.pop("attached_side", None)
                node.metadata["demoted_from"] = "boundary_event"
                continue
            if attached_to.node_type not in {
                NodeType.TASK,
                NodeType.USER_TASK,
                NodeType.SERVICE_TASK,
                NodeType.SUBPROCESS,
                NodeType.COLLAPSED_SUBPROCESS,
            }:
                node.node_type = NodeType.INTERMEDIATE_EVENT
                node.metadata.pop("attached_to", None)
                node.metadata.pop("attached_side", None)
                node.metadata["demoted_from"] = "boundary_event"
                continue
            if not _looks_attached_to_boundary(node, attached_to):
                node.node_type = NodeType.INTERMEDIATE_EVENT
                node.metadata.pop("attached_to", None)
                node.metadata.pop("attached_side", None)
                node.metadata["demoted_from"] = "boundary_event"

    def _merge_geometric_association_candidates(self, diagram: DiagramDocument) -> None:
        existing_pairs = {
            tuple(sorted((edge.source_id, edge.target_id)))
            for edge in diagram.edges
            if not edge.deleted
        }
        nodes_by_id = {node.id: node for node in diagram.nodes if not node.deleted}
        data_nodes = [
            node
            for node in diagram.nodes
            if not node.deleted and node.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}
        ]
        activity_nodes = [
            node
            for node in diagram.nodes
            if not node.deleted and node.node_type in {
                NodeType.TASK,
                NodeType.USER_TASK,
                NodeType.SERVICE_TASK,
                NodeType.SUBPROCESS,
                NodeType.COLLAPSED_SUBPROCESS,
            }
        ]
        for data_node in data_nodes:
            if any(
                not edge.deleted
                and edge.edge_type == EdgeType.ASSOCIATION
                and data_node.id in {edge.source_id, edge.target_id}
                for edge in diagram.edges
            ):
                continue
            best_activity: DiagramNode | None = None
            best_distance = float("inf")
            for activity in activity_nodes:
                if tuple(sorted((data_node.id, activity.id))) in existing_pairs:
                    continue
                if not _shares_business_context(data_node, activity, nodes_by_id):
                    continue
                distance = _axis_aligned_gap(data_node, activity)
                if distance > 170.0:
                    continue
                if distance < best_distance:
                    best_distance = distance
                    best_activity = activity
            if best_activity is None:
                continue
            start, end = _closest_connection_points(best_activity, data_node)
            diagram.edges.append(
                DiagramEdge(
                    id=f"edge-{uuid.uuid4().hex[:8]}",
                    edge_type=EdgeType.ASSOCIATION,
                    source_id=best_activity.id,
                    target_id=data_node.id,
                    confidence=0.42,
                    waypoints=[start, end],
                    metadata={"inferred_from": "geometric_association"},
                )
            )
            existing_pairs.add(tuple(sorted((data_node.id, best_activity.id))))

    def _prune_and_infer_sequence_flows(self, diagram: DiagramDocument) -> None:
        task_texts = {
            _normalize_text(node.text)
            for node in diagram.nodes
            if not node.deleted and node.node_type == NodeType.TASK and node.text.strip()
        }
        for node in diagram.nodes:
            if node.deleted or node.node_type not in {NodeType.POOL, NodeType.LANE}:
                continue
            normalized = _normalize_text(node.text)
            if normalized and normalized in task_texts:
                node.deleted = True

        nodes_by_id = {node.id: node for node in diagram.nodes}
        for edge in diagram.edges:
            if edge.deleted:
                continue
            source = nodes_by_id.get(edge.source_id)
            target = nodes_by_id.get(edge.target_id)
            if source is None or target is None or source.deleted or target.deleted:
                edge.deleted = True
                continue
            if source.node_type in {NodeType.POOL, NodeType.LANE} or target.node_type in {NodeType.POOL, NodeType.LANE}:
                edge.deleted = True

        valid_sequence_pairs = {
            (edge.source_id, edge.target_id)
            for edge in diagram.edges
            if not edge.deleted and edge.edge_type == EdgeType.SEQUENCE_FLOW
        }
        main_row = _find_primary_sequence_row(diagram)
        if len(main_row) < 2:
            return

        for left, right in zip(main_row, main_row[1:]):
            if (left.id, right.id) in valid_sequence_pairs:
                continue
            start, end = _closest_connection_points(left, right)
            diagram.edges.append(
                DiagramEdge(
                    id=f"edge-{uuid.uuid4().hex[:8]}",
                    edge_type=EdgeType.SEQUENCE_FLOW,
                    source_id=left.id,
                    target_id=right.id,
                    confidence=0.56,
                    waypoints=[start, end],
                    metadata={"inferred_from": "primary_sequence_row"},
                )
            )
            valid_sequence_pairs.add((left.id, right.id))

    def _infer_semantics(self, diagram: DiagramDocument, ocr_lines: list[OcrLine]) -> None:
        self._infer_container_semantics(diagram, ocr_lines)
        self._infer_node_types(diagram)
        self._infer_edge_types(diagram)
        self._sanitize_semantic_conflicts(diagram, ocr_lines)

    def _sanitize_semantic_conflicts(self, diagram: DiagramDocument, ocr_lines: list[OcrLine]) -> None:
        self._remove_false_header_data_stores(diagram, ocr_lines)
        self._repair_primary_row_activity_labels(diagram, ocr_lines)
        self._demote_invalid_start_events(diagram)
        self._remove_branch_label_nodes(diagram)

    def _remove_false_header_data_stores(self, diagram: DiagramDocument, ocr_lines: list[OcrLine]) -> None:
        connected_ids = {
            node_id
            for edge in diagram.edges
            if not edge.deleted
            for node_id in (edge.source_id, edge.target_id)
        }
        for node in diagram.nodes:
            if node.deleted or node.node_type != NodeType.DATA_STORE:
                continue
            normalized = _normalize_text(node.text)
            overlaps_header = any(
                _intersection_area(node.x, node.y, node.width, node.height, line.x, line.y, line.width, line.height) > 0
                and _is_global_document_header_text(line.text, line.y, diagram.image_height)
                for line in ocr_lines
            )
            if (
                node.y <= diagram.image_height * 0.22
                and (
                    not normalized
                    or overlaps_header
                    or node.id not in connected_ids
                    or not _looks_like_data_store_from_visuals(normalized)
                    or _looks_like_document_header_artifact(
                        node,
                        normalized,
                        diagram.image_width,
                        diagram.image_height,
                    )
                )
            ):
                node.deleted = True

    def _repair_primary_row_activity_labels(
        self, diagram: DiagramDocument, ocr_lines: list[OcrLine]
    ) -> None:
        primary_row_ids = {node.id for node in _find_primary_sequence_row(diagram)}
        if not primary_row_ids:
            return

        activity_types = {
            NodeType.TASK,
            NodeType.USER_TASK,
            NodeType.SERVICE_TASK,
            NodeType.SUBPROCESS,
            NodeType.COLLAPSED_SUBPROCESS,
        }
        for node in diagram.nodes:
            if node.deleted or node.id not in primary_row_ids or node.node_type not in activity_types:
                continue

            normalized = _normalize_text(node.text)
            if normalized and not _is_branch_label_text(normalized) and not _is_activity_code_text(normalized):
                continue

            candidate_lines = [
                line
                for line in ocr_lines
                if _intersection_area(
                    node.x - 8.0,
                    node.y - 8.0,
                    node.width + 16.0,
                    node.height + 16.0,
                    line.x,
                    line.y,
                    line.width,
                    line.height,
                ) > 0
            ]
            if not candidate_lines:
                continue

            repaired_text, metadata = _extract_node_text_from_ocr(
                node, candidate_lines, diagram.image_height
            )
            repaired_normalized = _normalize_text(repaired_text)
            if not repaired_normalized or _is_branch_label_text(repaired_normalized):
                continue
            node.text = repaired_text
            if metadata:
                node.metadata.update(metadata)

    def _demote_invalid_start_events(self, diagram: DiagramDocument) -> None:
        incoming: dict[str, int] = defaultdict(int)
        sources_by_target: dict[str, list[str]] = defaultdict(list)
        nodes_by_id = {node.id: node for node in diagram.nodes if not node.deleted}
        for edge in diagram.edges:
            if edge.deleted or edge.edge_type != EdgeType.SEQUENCE_FLOW:
                continue
            incoming[edge.target_id] += 1
            sources_by_target[edge.target_id].append(edge.source_id)

        for node in diagram.nodes:
            if node.deleted or node.node_type != NodeType.START_EVENT:
                continue
            if incoming.get(node.id, 0) <= 0:
                continue
            source_nodes = [nodes_by_id[source_id] for source_id in sources_by_target.get(node.id, []) if source_id in nodes_by_id]
            if any(source.node_type == NodeType.START_EVENT for source in source_nodes) or incoming.get(node.id, 0) > 0:
                normalized = _normalize_text(node.text)
                if _looks_like_service_task_text(normalized):
                    node.node_type = NodeType.SERVICE_TASK
                elif _looks_like_user_task_text(normalized):
                    node.node_type = NodeType.USER_TASK
                elif _looks_like_subprocess(node, normalized):
                    node.node_type = NodeType.SUBPROCESS
                else:
                    node.node_type = NodeType.TASK
                node.metadata["demoted_from"] = "start_event"
                node.confidence = min(max(node.confidence, 0.68) + 0.06, 0.95)

    def _remove_branch_label_nodes(self, diagram: DiagramDocument) -> None:
        branch_labels = {"si", "no", "yes", "true", "false"}
        removable_types = {
            NodeType.TASK,
            NodeType.USER_TASK,
            NodeType.SERVICE_TASK,
            NodeType.SUBPROCESS,
            NodeType.COLLAPSED_SUBPROCESS,
            NodeType.ANNOTATION,
            NodeType.DATA_OBJECT,
        }
        for node in diagram.nodes:
            if node.deleted or node.node_type not in removable_types:
                continue
            normalized = _normalize_text(node.text)
            if normalized in branch_labels:
                node.deleted = True

    def _infer_container_semantics(self, diagram: DiagramDocument, ocr_lines: list[OcrLine]) -> None:
        self._suppress_false_lanes(diagram)
        pools = [node for node in diagram.nodes if node.node_type == NodeType.POOL and not node.deleted]
        lanes = [node for node in diagram.nodes if node.node_type == NodeType.LANE and not node.deleted]

        synthetic_pool = self._apply_horizontal_lane_header_pattern(diagram, ocr_lines, pools, lanes)
        if synthetic_pool is not None:
            pools = [node for node in diagram.nodes if node.node_type == NodeType.POOL and not node.deleted]
            lanes = [node for node in diagram.nodes if node.node_type == NodeType.LANE and not node.deleted]

        if not pools and lanes:
            if len(lanes) == 1:
                largest_lane = max(lanes, key=lambda node: node.width * node.height)
                largest_lane.node_type = NodeType.POOL
                largest_lane.confidence = min(largest_lane.confidence + 0.08, 0.95)
                pools = [largest_lane]
                lanes = [node for node in lanes if node.id != largest_lane.id]
            else:
                left = min(node.x for node in lanes)
                top = min(node.y for node in lanes)
                right = max(node.x + node.width for node in lanes)
                bottom = max(node.y + node.height for node in lanes)
                synthetic_pool = DiagramNode(
                    id=f"node-{uuid.uuid4().hex[:8]}",
                    node_type=NodeType.POOL,
                    x=max(0.0, left - 18.0),
                    y=max(0.0, top - 18.0),
                    width=min(float(diagram.image_width), (right - left) + 36.0),
                    height=min(float(diagram.image_height), (bottom - top) + 36.0),
                    confidence=0.72,
                    metadata={"inferred_from": "multi_lane_pool"},
                )
                diagram.nodes.append(synthetic_pool)
                pools = [synthetic_pool]

        for lane in lanes:
            containing_pools = [
                pool for pool in pools if _node_contains_node(pool, lane)
            ]
            if containing_pools:
                lane.parent_id = min(
                    containing_pools, key=lambda node: node.width * node.height
                ).id

        for node in diagram.nodes:
            if node.deleted:
                continue
            if node.node_type in {NodeType.POOL, NodeType.LANE}:
                continue
            node.parent_id = None
            containing_lanes = [lane for lane in lanes if _node_contains_point(lane, node.center.x, node.center.y)]
            if containing_lanes:
                node.parent_id = min(containing_lanes, key=lambda item: item.width * item.height).id
                continue
            containing_pools = [pool for pool in pools if _node_contains_point(pool, node.center.x, node.center.y)]
            if containing_pools:
                node.parent_id = min(containing_pools, key=lambda item: item.width * item.height).id

        for container in pools + lanes:
            if container.text.strip():
                continue
            container.text = _best_container_label(container, ocr_lines)

    def _suppress_false_lanes(self, diagram: DiagramDocument) -> None:
        candidate_nodes = [
            node
            for node in diagram.nodes
            if not node.deleted and node.node_type not in {NodeType.POOL, NodeType.LANE}
        ]
        for lane in [node for node in diagram.nodes if node.node_type == NodeType.LANE and not node.deleted]:
            normalized = _normalize_text(lane.text)
            if "responsable de" in normalized or "encargado de" in normalized:
                continue
            if lane.width > max(56.0, diagram.image_width * 0.08):
                continue
            if lane.height < diagram.image_height * 0.28:
                continue
            contained_nodes = [
                node for node in candidate_nodes
                if _node_contains_point(lane, node.center.x, node.center.y)
            ]
            if contained_nodes:
                continue
            lane.deleted = True

    def _apply_horizontal_lane_header_pattern(
        self,
        diagram: DiagramDocument,
        ocr_lines: list[OcrLine],
        pools: list[DiagramNode],
        lanes: list[DiagramNode],
    ) -> DiagramNode | None:
        candidate_lanes = [
            node
            for node in lanes
            if node.width >= diagram.image_width * 0.22
            and node.height >= diagram.image_height * 0.22
            and node.y >= diagram.image_height * 0.10
        ]
        if len(candidate_lanes) < 2:
            return None

        candidate_lanes.sort(key=lambda node: (node.y, node.x))
        top_anchor = min(node.y for node in candidate_lanes)
        row_lanes = [node for node in candidate_lanes if abs(node.y - top_anchor) <= max(24.0, diagram.image_height * 0.03)]
        if len(row_lanes) < 2:
            return None

        lane_labels: dict[str, str] = {}
        responsible_hits = 0
        for lane in row_lanes:
            label = _find_lane_header_label(lane, ocr_lines)
            if label:
                lane_labels[lane.id] = label
                if "responsable de" in _normalize_text(label):
                    responsible_hits += 1
        if responsible_hits < 2:
            return None

        pool_title = _extract_process_identifier_title(ocr_lines, diagram.image_height)
        left = min(node.x for node in row_lanes)
        top = min(node.y for node in row_lanes)
        right = max(node.x + node.width for node in row_lanes)
        bottom = max(node.y + node.height for node in row_lanes)

        pool = min(
            (
                node
                for node in pools
                if _node_contains_bounds(node, left, top, right - left, bottom - top)
            ),
            key=lambda node: node.width * node.height,
            default=None,
        )
        if pool is None:
            pool = DiagramNode(
                id=f"node-{uuid.uuid4().hex[:8]}",
                node_type=NodeType.POOL,
                x=max(0.0, left - 18.0),
                y=max(0.0, top - 18.0),
                width=min(float(diagram.image_width), (right - left) + 36.0),
                height=min(float(diagram.image_height), (bottom - top) + 36.0),
                text=pool_title,
                confidence=0.84,
                metadata={"inferred_from": "horizontal_lane_header_pattern"},
            )
            diagram.nodes.append(pool)
        else:
            pool.text = pool_title or pool.text
            pool.confidence = min(max(pool.confidence, 0.8) + 0.04, 0.95)
            pool.metadata["inferred_from"] = "horizontal_lane_header_pattern"

        for lane in row_lanes:
            lane.parent_id = pool.id
            if lane.id in lane_labels:
                lane.text = lane_labels[lane.id]
                lane.confidence = min(max(lane.confidence, 0.74) + 0.08, 0.95)
                lane.metadata["inferred_from"] = "lane_header_label"
        return pool

    def _infer_node_types(self, diagram: DiagramDocument) -> None:
        primary_row_ids = {node.id for node in _find_primary_sequence_row(diagram)}
        for node in diagram.nodes:
            if node.deleted:
                continue
            normalized_text = _normalize_text(node.text)
            if node.node_type == NodeType.SUBPROCESS and _has_collapsed_marker(node):
                node.node_type = NodeType.COLLAPSED_SUBPROCESS
                node.confidence = min(node.confidence + 0.05, 0.95)
                continue
            if not normalized_text:
                continue
            if _looks_like_start_event_text(normalized_text):
                node.node_type = NodeType.START_EVENT
                node.confidence = min(max(node.confidence, 0.68) + 0.1, 0.95)
                continue
            if _looks_like_end_event_text(normalized_text):
                node.node_type = NodeType.END_EVENT
                node.confidence = min(max(node.confidence, 0.68) + 0.1, 0.95)
                continue
            in_primary_row = node.id in primary_row_ids
            if node.node_type in {NodeType.TASK, NodeType.SUBPROCESS, NodeType.COLLAPSED_SUBPROCESS}:
                if _looks_like_note_box(normalized_text, node) and not in_primary_row:
                    node.node_type = NodeType.ANNOTATION
                    node.confidence = min(max(node.confidence, 0.64) + 0.08, 0.95)
                    continue
            if node.node_type == NodeType.TASK:
                if (node.metadata or {}).get("force_task"):
                    node.confidence = min(max(node.confidence, 0.72) + 0.05, 0.95)
                    continue
                if in_primary_row:
                    if _looks_like_service_task_text(normalized_text):
                        node.node_type = NodeType.SERVICE_TASK
                        node.confidence = min(node.confidence + 0.08, 0.95)
                    elif _looks_like_user_task_text(normalized_text):
                        node.node_type = NodeType.USER_TASK
                        node.confidence = min(node.confidence + 0.08, 0.95)
                    elif _looks_like_subprocess(node, normalized_text):
                        node.node_type = NodeType.SUBPROCESS
                        node.confidence = min(node.confidence + 0.06, 0.95)
                    else:
                        node.confidence = min(max(node.confidence, 0.72) + 0.04, 0.95)
                    continue
                if _looks_like_data_store(node, normalized_text):
                    node.node_type = NodeType.DATA_STORE
                    node.confidence = min(node.confidence + 0.12, 0.95)
                    continue
                if _looks_like_data_object_text(normalized_text):
                    node.node_type = NodeType.DATA_OBJECT
                    node.confidence = min(node.confidence + 0.1, 0.95)
                    continue
                if _looks_like_annotation_text(normalized_text, node, in_primary_row=in_primary_row):
                    node.node_type = NodeType.ANNOTATION
                    node.confidence = min(node.confidence + 0.08, 0.95)
                    continue
                if _looks_like_service_task_text(normalized_text):
                    node.node_type = NodeType.SERVICE_TASK
                    node.confidence = min(node.confidence + 0.08, 0.95)
                    continue
                if _looks_like_user_task_text(normalized_text):
                    node.node_type = NodeType.USER_TASK
                    node.confidence = min(node.confidence + 0.08, 0.95)
                    continue
                if _looks_like_subprocess(node, normalized_text):
                    node.node_type = NodeType.SUBPROCESS
                    node.confidence = min(node.confidence + 0.06, 0.95)
            elif node.node_type in {NodeType.EXCLUSIVE_GATEWAY, NodeType.PARALLEL_GATEWAY}:
                refined_type = _refine_gateway_type(node, normalized_text)
                if refined_type != node.node_type:
                    node.node_type = refined_type
                    node.confidence = min(node.confidence + 0.06, 0.95)

    def _infer_edge_types(self, diagram: DiagramDocument) -> None:
        nodes_by_id = {node.id: node for node in diagram.nodes}
        for edge in diagram.edges:
            if edge.deleted:
                continue
            source = nodes_by_id.get(edge.source_id)
            target = nodes_by_id.get(edge.target_id)
            if not source or not target:
                continue

            if _is_association_candidate(source, target, edge):
                edge.edge_type = EdgeType.ASSOCIATION
                edge.confidence = min(edge.confidence + 0.1, 0.95)
                continue

            source_pool = _find_ancestor_container(source, nodes_by_id, NodeType.POOL)
            target_pool = _find_ancestor_container(target, nodes_by_id, NodeType.POOL)
            if (
                source_pool
                and target_pool
                and source_pool.id != target_pool.id
                and _is_message_flow_candidate(source, target, edge)
            ):
                edge.edge_type = EdgeType.MESSAGE_FLOW
                edge.confidence = min(edge.confidence + 0.08, 0.95)
                continue

            edge.edge_type = EdgeType.SEQUENCE_FLOW

    def _validate_semantics(self, diagram: DiagramDocument) -> None:
        nodes_by_id = {node.id: node for node in diagram.nodes}
        existing_keys = {
            (issue.related_kind, issue.related_id, issue.message)
            for issue in diagram.issues
        }

        for edge in diagram.edges:
            if edge.deleted:
                continue
            source = nodes_by_id.get(edge.source_id)
            target = nodes_by_id.get(edge.target_id)
            if not source or not target:
                continue

            if edge.edge_type == EdgeType.SEQUENCE_FLOW:
                if source.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION} or target.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
                    edge.edge_type = EdgeType.ASSOCIATION
                    edge.confidence = min(edge.confidence + 0.05, 0.95)
                source_pool = _find_ancestor_container(source, nodes_by_id, NodeType.POOL)
                target_pool = _find_ancestor_container(target, nodes_by_id, NodeType.POOL)
                if source_pool and target_pool and source_pool.id != target_pool.id:
                    self._append_issue(
                        diagram,
                        existing_keys,
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.WARNING,
                            message="Un sequence flow no deberia cruzar pools distintos.",
                            related_kind="edge",
                            related_id=edge.id,
                        ),
                    )
                    edge.confidence = min(edge.confidence, 0.45)
                if source.node_type == NodeType.END_EVENT:
                    self._append_issue(
                        diagram,
                        existing_keys,
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.WARNING,
                            message="Un end event no deberia tener flujo saliente de secuencia.",
                            related_kind="edge",
                            related_id=edge.id,
                        ),
                    )
                    edge.confidence = min(edge.confidence, 0.45)
                if target.node_type == NodeType.START_EVENT:
                    self._append_issue(
                        diagram,
                        existing_keys,
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.WARNING,
                            message="Un start event no deberia tener flujo entrante de secuencia.",
                            related_kind="edge",
                            related_id=edge.id,
                        ),
                    )
                    edge.confidence = min(edge.confidence, 0.45)
            elif edge.edge_type == EdgeType.MESSAGE_FLOW:
                source_pool = _find_ancestor_container(source, nodes_by_id, NodeType.POOL)
                target_pool = _find_ancestor_container(target, nodes_by_id, NodeType.POOL)
                if not source_pool or not target_pool or source_pool.id == target_pool.id:
                    self._append_issue(
                        diagram,
                        existing_keys,
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.WARNING,
                            message="Un message flow deberia conectar pools distintos.",
                            related_kind="edge",
                            related_id=edge.id,
                        ),
                    )
                    edge.confidence = min(edge.confidence, 0.45)
            elif edge.edge_type == EdgeType.ASSOCIATION:
                if source.node_type not in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION} and target.node_type not in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
                    self._append_issue(
                        diagram,
                        existing_keys,
                        ReviewIssue(
                            id=f"issue-{uuid.uuid4().hex[:8]}",
                            severity=IssueSeverity.INFO,
                            message="La asociacion no toca ni objeto de datos ni anotacion; revisar si deberia ser sequence flow.",
                            related_kind="edge",
                            related_id=edge.id,
                        ),
                    )

        for node in diagram.nodes:
            if node.deleted or node.node_type != NodeType.BOUNDARY_EVENT:
                continue
            attached_to_id = str((node.metadata or {}).get("attached_to") or "").strip()
            attached_to = nodes_by_id.get(attached_to_id) if attached_to_id else None
            if not attached_to or attached_to.deleted:
                self._append_issue(
                    diagram,
                    existing_keys,
                    ReviewIssue(
                        id=f"issue-{uuid.uuid4().hex[:8]}",
                        severity=IssueSeverity.WARNING,
                        message="El boundary event no tiene actividad adjunta valida.",
                        related_kind="node",
                        related_id=node.id,
                    ),
                )
                node.confidence = min(node.confidence, 0.4)
                continue
            if not _looks_attached_to_boundary(node, attached_to):
                self._append_issue(
                    diagram,
                    existing_keys,
                    ReviewIssue(
                        id=f"issue-{uuid.uuid4().hex[:8]}",
                        severity=IssueSeverity.WARNING,
                        message="El boundary event no parece estar pegado al borde de la actividad.",
                        related_kind="node",
                        related_id=node.id,
                    ),
                )
                node.confidence = min(node.confidence, 0.45)

        incoming: dict[str, int] = defaultdict(int)
        outgoing: dict[str, int] = defaultdict(int)
        for edge in diagram.edges:
            if edge.deleted or edge.edge_type != EdgeType.SEQUENCE_FLOW:
                continue
            outgoing[edge.source_id] += 1
            incoming[edge.target_id] += 1

        for node in diagram.nodes:
            if node.deleted:
                continue
            if node.node_type == NodeType.START_EVENT and incoming.get(node.id, 0) > 0:
                self._append_issue(
                    diagram,
                    existing_keys,
                    ReviewIssue(
                        id=f"issue-{uuid.uuid4().hex[:8]}",
                        severity=IssueSeverity.WARNING,
                        message="El start event tiene flujos entrantes; revisar semantica.",
                        related_kind="node",
                        related_id=node.id,
                    ),
                )
                node.confidence = min(node.confidence, 0.45)
            if node.node_type == NodeType.END_EVENT and outgoing.get(node.id, 0) > 0:
                self._append_issue(
                    diagram,
                    existing_keys,
                    ReviewIssue(
                        id=f"issue-{uuid.uuid4().hex[:8]}",
                        severity=IssueSeverity.WARNING,
                        message="El end event tiene flujos salientes; revisar semantica.",
                        related_kind="node",
                        related_id=node.id,
                    ),
                )
                node.confidence = min(node.confidence, 0.45)

    def _append_issue(
        self,
        diagram: DiagramDocument,
        existing_keys: set[tuple[str | None, str | None, str]],
        issue: ReviewIssue,
    ) -> None:
        key = (issue.related_kind, issue.related_id, issue.message)
        if key in existing_keys:
            return
        diagram.issues.append(issue)
        existing_keys.add(key)


def _read_image_dimensions(image_path: Path) -> tuple[int, int]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para leer dimensiones de imagen.") from exc

    image = cv2_imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"No se pudo abrir la imagen rasterizada: {image_path}")
    height, width = image.shape[:2]
    return width, height


def _infer_diagram_title(diagram: DiagramDocument, lines: list[OcrLine]) -> str:
    candidates: list[tuple[float, str]] = []
    top_limit = max(diagram.image_height * 0.22, 120.0)
    for line in lines:
        text = " ".join(line.text.split()).strip()
        if not text:
            continue
        if line.y > top_limit:
            continue
        if len(text) > 60:
            continue
        if any(char.isdigit() for char in text):
            continue
        if text.lower().startswith("mpa-"):
            continue
        letters = [char for char in text if char.isalpha()]
        if not letters:
            continue
        uppercase_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
        score = (line.height * 4.0) + (uppercase_ratio * 10.0) - (line.y / 100.0)
        candidates.append((score, text))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    if diagram.source_pdf.stem:
        stem = diagram.source_pdf.stem.replace("-", " ").replace("_", " ").strip()
        return " ".join(part.capitalize() for part in stem.split())
    return "Diagrama BPMN"


def _prepare_sketch_ocr_lines(lines: list[OcrLine]) -> list[OcrLine]:
    if not lines:
        return []
    ordered = sorted(lines, key=lambda item: (item.y, item.x))
    merged: list[OcrLine] = []
    for line in ordered:
        text = " ".join(line.text.split()).strip()
        if not text:
            continue
        if not merged:
            merged.append(
                OcrLine(
                    text=text,
                    x=line.x,
                    y=line.y,
                    width=line.width,
                    height=line.height,
                    confidence=line.confidence,
                )
            )
            continue
        previous = merged[-1]
        y_gap = abs(previous.center[1] - line.center[1])
        x_gap = line.x - (previous.x + previous.width)
        height_ratio = min(previous.height, line.height) / max(previous.height, line.height, 1.0)
        if y_gap <= max(previous.height, line.height) * 0.75 and -12.0 <= x_gap <= 36.0 and height_ratio >= 0.55:
            previous.text = f"{previous.text} {text}".strip()
            previous.width = max(previous.x + previous.width, line.x + line.width) - previous.x
            previous.height = max(previous.y + previous.height, line.y + line.height) - min(previous.y, line.y)
            previous.y = min(previous.y, line.y)
            if previous.confidence is not None and line.confidence is not None:
                previous.confidence = min(previous.confidence, line.confidence)
            continue
        merged.append(
            OcrLine(
                text=text,
                x=line.x,
                y=line.y,
                width=line.width,
                height=line.height,
                confidence=line.confidence,
            )
        )
    return merged


def _detect_node_candidates(
    image_path: Path, image_width: int, image_height: int, sketch_mode: bool = False
) -> tuple[list[DiagramNode], bool]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para detectar formas BPMN.") from exc

    image = cv2_imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"No se pudo abrir la imagen rasterizada: {image_path}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur_kernel = (7, 7) if sketch_mode else (5, 5)
    blur = cv2.GaussianBlur(gray, blur_kernel, 0)
    binary = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35 if sketch_mode else 31,
        8 if sketch_mode else 11,
    )
    if sketch_mode:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    nodes: list[DiagramNode] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < (900 if sketch_mode else 1200):
            continue
        x, y, width, height = cv2.boundingRect(contour)
        if width < (24 if sketch_mode else 28) or height < (16 if sketch_mode else 18):
            continue
        if width > image_width * 0.95 and height > image_height * 0.95:
            continue

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, (0.045 if sketch_mode else 0.03) * perimeter, True)
        fill_ratio = area / max(width * height, 1)
        node_type = _classify_contour(
            width,
            height,
            len(approx),
            fill_ratio,
            image_width,
            sketch_mode=sketch_mode,
        )
        confidence = _initial_confidence(node_type, fill_ratio)
        if sketch_mode:
            confidence = min(max(confidence, 0.52) + 0.03, 0.9)
        nodes.append(
            DiagramNode(
                id=f"node-{uuid.uuid4().hex[:8]}",
                node_type=node_type,
                x=float(x),
                y=float(y),
                width=float(width),
                height=float(height),
                confidence=confidence,
            )
        )

    nodes = _dedupe_nodes(nodes)
    nodes = _merge_stacked_task_segments(nodes, image_height)
    nodes = _suppress_banded_task_fragments(nodes)
    _assign_container_relationships(nodes)
    return nodes, bool(contours)


def _classify_contour(
    width: int,
    height: int,
    vertices: int,
    fill_ratio: float,
    image_width: int,
    sketch_mode: bool = False,
) -> NodeType:
    aspect_ratio = width / max(height, 1)
    if aspect_ratio > 5.5 and width > image_width * 0.30:
        return NodeType.LANE
    if aspect_ratio > 2.2 and height > 80 and width > image_width * 0.50:
        return NodeType.POOL
    if vertices >= 8 and 0.7 <= aspect_ratio <= 1.8 and width >= 55 and height >= 40:
        return NodeType.DATA_STORE
    if vertices == 4 and fill_ratio < 0.72:
        return NodeType.EXCLUSIVE_GATEWAY
    if vertices >= 7 and 0.55 <= fill_ratio <= 0.95:
        return NodeType.START_EVENT
    if sketch_mode:
        if 0.7 <= aspect_ratio <= 1.4 and width >= 26 and height >= 26 and vertices >= 5:
            return NodeType.START_EVENT
        if 0.7 <= aspect_ratio <= 1.35 and width >= 30 and height >= 30 and 4 <= vertices <= 6:
            return NodeType.EXCLUSIVE_GATEWAY
    return NodeType.TASK


def _initial_confidence(node_type: NodeType, fill_ratio: float) -> float:
    base = {
        NodeType.TASK: 0.62,
        NodeType.START_EVENT: 0.58,
        NodeType.EXCLUSIVE_GATEWAY: 0.54,
        NodeType.POOL: 0.48,
        NodeType.LANE: 0.46,
        NodeType.DATA_STORE: 0.56,
    }.get(node_type, 0.45)
    return min(0.99, max(0.2, base + (fill_ratio - 0.5) * 0.25))


def _dedupe_nodes(nodes: list[DiagramNode]) -> list[DiagramNode]:
    deduped: list[DiagramNode] = []
    for candidate in sorted(nodes, key=lambda item: item.width * item.height, reverse=True):
        if any(_iou(candidate, existing) > 0.85 for existing in deduped):
            continue
        deduped.append(candidate)
    return list(reversed(deduped))


def _merge_stacked_task_segments(nodes: list[DiagramNode], image_height: int) -> list[DiagramNode]:
    ordered = sorted(nodes, key=lambda item: (item.x, item.y, item.width, item.height))
    consumed: set[str] = set()
    merged: list[DiagramNode] = []

    for node in ordered:
        if node.id in consumed:
            continue
        if node.node_type != NodeType.TASK or node.y <= image_height * 0.18:
            merged.append(node)
            consumed.add(node.id)
            continue

        group = [node]
        current = node
        while True:
            next_node = _find_stacked_segment_candidate(current, ordered, consumed)
            if next_node is None:
                break
            group.append(next_node)
            current = next_node

        if len(group) < 2:
            merged.append(node)
            consumed.add(node.id)
            continue

        total_height = max(item.y + item.height for item in group) - min(item.y for item in group)
        if total_height < 70 or total_height > 170:
            merged.append(node)
            consumed.add(node.id)
            continue

        consumed.update(item.id for item in group)
        merged.append(
            DiagramNode(
                id=f"node-{uuid.uuid4().hex[:8]}",
                node_type=NodeType.TASK,
                x=min(item.x for item in group),
                y=min(item.y for item in group),
                width=max(item.x + item.width for item in group) - min(item.x for item in group),
                height=max(item.y + item.height for item in group) - min(item.y for item in group),
                confidence=min(max(max(item.confidence for item in group), 0.68) + 0.08, 0.95),
                metadata={
                    "merged_segments": [item.id for item in group],
                    "force_task": True,
                    "layout_hint": "banded_activity",
                },
            )
        )

    return _dedupe_nodes(merged)


def _find_stacked_segment_candidate(
    node: DiagramNode,
    ordered: list[DiagramNode],
    consumed: set[str],
) -> DiagramNode | None:
    best_candidate: DiagramNode | None = None
    best_gap = float("inf")
    for candidate in ordered:
        if candidate.id == node.id or candidate.id in consumed or candidate.node_type != NodeType.TASK:
            continue
        if candidate.y < node.y + node.height - 2:
            continue
        gap = candidate.y - (node.y + node.height)
        if gap > 14:
            continue
        x_offset = abs(candidate.x - node.x)
        width_delta = abs(candidate.width - node.width)
        horizontal_overlap = min(node.x + node.width, candidate.x + candidate.width) - max(node.x, candidate.x)
        if horizontal_overlap <= min(node.width, candidate.width) * 0.82:
            continue
        if x_offset > 14 or width_delta > max(node.width, candidate.width) * 0.18:
            continue
        if gap < best_gap:
            best_gap = gap
            best_candidate = candidate
    return best_candidate


def _suppress_banded_task_fragments(nodes: list[DiagramNode]) -> list[DiagramNode]:
    merged_tasks = [
        node
        for node in nodes
        if node.node_type == NodeType.TASK and (node.metadata or {}).get("layout_hint") == "banded_activity"
    ]
    if not merged_tasks:
        return nodes

    filtered: list[DiagramNode] = []
    for node in nodes:
        if any(_is_banded_task_fragment(node, merged_task) for merged_task in merged_tasks):
            continue
        filtered.append(node)
    return filtered


def _is_banded_task_fragment(node: DiagramNode, merged_task: DiagramNode) -> bool:
    if node.id == merged_task.id or node.node_type != NodeType.TASK:
        return False
    if node.height > 34 or merged_task.height < 80:
        return False
    if node.center.y >= merged_task.center.y:
        return False
    horizontal_overlap = min(node.x + node.width, merged_task.x + merged_task.width) - max(node.x, merged_task.x)
    if horizontal_overlap <= min(node.width, merged_task.width) * 0.82:
        return False
    vertical_gap = merged_task.y - (node.y + node.height)
    if not (-4 <= vertical_gap <= 10):
        return False
    return abs(node.center.x - merged_task.center.x) <= max(18.0, merged_task.width * 0.08)


def _assign_container_relationships(nodes: list[DiagramNode]) -> None:
    containers = [node for node in nodes if node.node_type in {NodeType.POOL, NodeType.LANE}]
    for node in nodes:
        if node in containers:
            continue
        center = node.center
        containing = [
            container
            for container in containers
            if container.x <= center.x <= container.x + container.width
            and container.y <= center.y <= container.y + container.height
        ]
        if containing:
            containing.sort(key=lambda item: item.width * item.height)
            node.parent_id = containing[0].id


def _find_best_container(nodes: list[DiagramNode], line: OcrLine) -> DiagramNode | None:
    cx, cy = line.center
    matches: list[tuple[float, float, float, DiagramNode]] = []
    line_area = max(line.width * line.height, 1.0)
    for node in nodes:
        if node.deleted:
            continue
        overlap = _intersection_area(
            node.x,
            node.y,
            node.width,
            node.height,
            line.x,
            line.y,
            line.width,
            line.height,
        )
        contains_center = node.x <= cx <= node.x + node.width and node.y <= cy <= node.y + node.height
        if overlap <= 0 and not contains_center:
            continue
        overlap_ratio = overlap / line_area
        distance = abs(node.center.x - cx) + abs(node.center.y - cy)
        area = node.width * node.height
        matches.append((overlap_ratio, 1.0 if contains_center else 0.0, -distance - (area * 0.00001), node))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return matches[0][3]


def _best_container_label(container: DiagramNode, lines: list[OcrLine]) -> str:
    candidates: list[tuple[float, str]] = []
    for line in lines:
        text = " ".join(line.text.split()).strip()
        if not text:
            continue
        overlap = _intersection_area(
            container.x,
            container.y,
            container.width,
            container.height,
            line.x,
            line.y,
            line.width,
            line.height,
        )
        if overlap <= 0:
            continue
        score = overlap
        if line.height > line.width:
            score *= 1.4
        if line.x <= container.x + max(container.width * 0.18, 60):
            score *= 1.2
        candidates.append((score, text))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _collect_annotation_lines(node: DiagramNode, lines: list[OcrLine]) -> list[OcrLine]:
    collected: list[OcrLine] = []
    for line in lines:
        text = " ".join(line.text.split()).strip()
        if not text:
            continue
        overlap = _intersection_area(
            node.x,
            node.y,
            node.width,
            node.height,
            line.x,
            line.y,
            line.width,
            line.height,
        )
        line_area = max(line.width * line.height, 1.0)
        overlap_ratio = overlap / line_area
        cx, cy = line.center
        center_inside = node.x <= cx <= node.x + node.width and node.y <= cy <= node.y + node.height
        if center_inside or overlap_ratio >= 0.35:
            collected.append(line)
    collected.sort(key=lambda item: (item.y, item.x, -item.width))
    return collected


def _find_lane_header_label(container: DiagramNode, lines: list[OcrLine]) -> str:
    candidates: list[tuple[float, str]] = []
    header_limit = container.y + min(container.height * 0.18, 72.0)
    for line in lines:
        text = " ".join(line.text.split()).strip()
        normalized = _normalize_text(text)
        if not text or not normalized:
            continue
        if line.y > header_limit:
            continue
        overlap = _intersection_area(
            container.x,
            container.y,
            container.width,
            container.height,
            line.x,
            line.y,
            line.width,
            line.height,
        )
        if overlap <= 0:
            continue
        if "responsable de" not in normalized:
            continue
        score = overlap + (line.width * 0.2) + (line.height * 2.0)
        candidates.append((score, text))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_process_identifier_title(lines: list[OcrLine], image_height: int) -> str:
    top_lines = [
        line
        for line in lines
        if line.text.strip() and line.y <= image_height * 0.12
    ]
    if not top_lines:
        return ""

    identifier_line = next(
        (line for line in sorted(top_lines, key=lambda item: (item.y, item.x)) if "proceso identificado" in _normalize_text(line.text)),
        None,
    )
    code_line = next(
        (
            line
            for line in sorted(top_lines, key=lambda item: (item.y, item.x))
            if re.search(r"\bmp[a-z]-p?\d{1,3}\b", _normalize_text(line.text))
        ),
        None,
    )
    if code_line is None:
        return ""

    title_candidates = [
        line
        for line in top_lines
        if line.x > code_line.x
        and line.y >= code_line.y - max(8.0, code_line.height * 0.8)
        and line.y <= code_line.y + max(18.0, code_line.height * 1.4)
        and _normalize_text(line.text) not in {"proceso identificado", _normalize_text(code_line.text)}
        and not re.search(r"\bmp[a-z]-p?\d{1,3}\b", _normalize_text(line.text))
    ]
    title_candidates.sort(key=lambda item: (item.x, item.y))
    title_text = " ".join(" ".join(line.text.split()).strip() for line in title_candidates).strip()
    code_text = " ".join(code_line.text.split()).strip()
    if title_text:
        return f"{code_text} {title_text}".strip()
    if identifier_line is not None:
        return code_text
    return code_text


def _extract_node_text_from_ocr(
    node: DiagramNode,
    lines: list[OcrLine],
    image_height: int,
) -> tuple[str, dict[str, object]]:
    ordered = sorted(lines, key=lambda item: (item.y, item.x, -item.width))
    metadata: dict[str, object] = {}
    if node.node_type in {NodeType.POOL, NodeType.LANE}:
        values = [" ".join(line.text.split()).strip() for line in ordered if line.text.strip()]
        return " ".join(values).strip(), metadata
    if node.node_type == NodeType.ANNOTATION:
        values = [" ".join(line.text.split()).strip() for line in ordered if line.text.strip()]
        if values:
            metadata["multiline_text"] = True
        return "\n".join(values).strip(), metadata

    kept_lines: list[OcrLine] = []
    ignored_regions: set[str] = set()
    header_hits = 0
    footer_hits = 0

    for line in ordered:
        text = " ".join(line.text.split()).strip()
        if not text:
            continue
        normalized = _normalize_text(text)
        if _is_global_document_header_text(text, line.y, image_height):
            ignored_regions.add("document_header")
            continue
        if node.node_type not in {
            NodeType.EXCLUSIVE_GATEWAY,
            NodeType.PARALLEL_GATEWAY,
            NodeType.INCLUSIVE_GATEWAY,
            NodeType.EVENT_BASED_GATEWAY,
        } and _is_branch_label_text(normalized):
            ignored_regions.add("branch_label")
            continue
        if node.node_type not in EVENT_NODE_TYPES and normalized in {"inicio", "inicia", "fin", "final"}:
            ignored_regions.add("foreign_event_label")
            continue
        region = _line_vertical_region(node, line)
        if _is_ignored_footer_text(text):
            ignored_regions.add(region)
            footer_hits += 1
            continue
        if _is_ignored_header_text(text, region):
            ignored_regions.add(region)
            header_hits += 1
            continue
        kept_lines.append(line)

    preferred = [line for line in kept_lines if _line_vertical_region(node, line) == "center"]
    if (node.metadata or {}).get("layout_hint") == "banded_activity" and preferred:
        selected = preferred
    else:
        selected = preferred or kept_lines
    values = [" ".join(line.text.split()).strip() for line in selected if line.text.strip()]
    normalized_values = [_normalize_text(value) for value in values if value]

    if node.node_type in {
        NodeType.TASK,
        NodeType.USER_TASK,
        NodeType.SERVICE_TASK,
        NodeType.SUBPROCESS,
        NodeType.COLLAPSED_SUBPROCESS,
    }:
        code_values = [value for value, normalized in zip(values, normalized_values) if _is_activity_code_text(normalized)]
        descriptive_values = [
            value
            for value, normalized in zip(values, normalized_values)
            if normalized and not _is_activity_code_text(normalized) and not _is_branch_label_text(normalized)
        ]
        if descriptive_values and code_values:
            values = code_values[:1] + descriptive_values
        elif descriptive_values:
            values = descriptive_values
        elif code_values:
            values = code_values[:1]

    if header_hits and footer_hits and node.node_type == NodeType.TASK:
        metadata["force_task"] = True
        metadata["layout_hint"] = "banded_activity"
    if ignored_regions:
        metadata["ignored_text_regions"] = sorted(ignored_regions)

    return " ".join(values).strip(), metadata


def _line_vertical_region(node: DiagramNode, line: OcrLine) -> str:
    if node.height <= 0:
        return "center"
    relative_center = ((line.y + (line.height / 2.0)) - node.y) / node.height
    top_threshold = 0.26
    bottom_threshold = 0.78
    if (node.metadata or {}).get("layout_hint") == "banded_activity":
        top_threshold = 0.34
        bottom_threshold = 0.74
    if relative_center <= top_threshold:
        return "top"
    if relative_center >= bottom_threshold:
        return "bottom"
    return "center"


def _is_ignored_header_text(text: str, region: str) -> bool:
    if region != "top":
        return False
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if "|" in text:
        return True
    if _is_activity_code_text(normalized):
        return True
    if re.search(r"\b\d{1,3}$", normalized):
        return True
    if re.search(r"\b[a-z]{2,4}\s*0\d{1,2}\b", normalized):
        return True
    return any(
        term in normalized
        for term in (
            "cuentas por cobrar",
            "cuentas por pagar",
            "proveedor",
            "portal",
            "tesoreria",
            "contabilidad",
            "responsable",
        )
    )


def _is_ignored_footer_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    return normalized in {"responsable", "cailero responsable", "caiero responsable"}


def _is_branch_label_text(normalized: str) -> bool:
    return normalized in {"si", "no", "yes", "true", "false"}


def _is_activity_code_text(normalized: str) -> bool:
    if not normalized:
        return False
    compact = normalized.replace(" ", "")
    return bool(
        re.fullmatch(r"(sp|mp[afc]?|ap|rp|op|bp)\d{1,3}", compact)
        or re.fullmatch(r"(sp|mp[afc]?|ap|rp|op|bp)-?\d{1,3}", compact)
    )


def _is_global_document_header_text(text: str, y: float, image_height: int) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if y > image_height * 0.22:
        return False
    if any(
        term in normalized
        for term in (
            "macro proceso",
            "macro proceso general",
            "proceso identificado",
            "integracion para",
            "facturacion electronica",
            "san juan bautista",
            "universidad privada",
            "sistema de",
            "sjb",
        )
    ):
        return True
    if re.search(r"\bmp[a-z]-p?\d{1,3}\b", normalized):
        return True
    return False


def _normalize_text(value: str) -> str:
    compact = " ".join(value.split()).strip().lower()
    if not compact:
        return ""
    return unicodedata.normalize("NFKD", compact).encode("ascii", "ignore").decode("ascii")


def _looks_like_start_event_text(text: str) -> bool:
    return text in {"inicio", "inicia"}


def _looks_like_end_event_text(text: str) -> bool:
    return text in {"fin", "final", "finalizacion"}


def _looks_like_document_header_artifact(
    node: DiagramNode,
    text: str,
    image_width: int,
    image_height: int,
) -> bool:
    top_band = node.y <= image_height * 0.22
    very_top_band = node.y <= image_height * 0.16
    if text:
        if any(
            term in text
            for term in (
                "macro proceso",
                "macro proceso general",
                "proceso identificado",
                "integracion para",
                "facturacion electronica",
                "universidad",
                "san juan bautista",
                "sistema de",
                "logo",
                "sjb",
                "mpc",
                "mpa",
                "mpf",
                "comercial",
            )
        ):
            return True
        if top_band and re.search(r"\b[a-z]{2,4}-p?\d{1,3}\b", text):
            return True
        if very_top_band and node.width > image_width * 0.18 and node.height < image_height * 0.14:
            return True
        if top_band and node.width > image_width * 0.22 and node.height < 70 and any(
            token in text
            for token in (
                "gestion de",
                "proceso",
                "macro",
                "identificado",
                "integracion",
                "facturacion",
                "universidad",
                "sistema",
            )
        ):
            return True
    if top_band and not text and node.width < 150 and node.height < 50:
        return True
    if very_top_band and node.height < image_height * 0.14 and node.width > image_width * 0.14:
        return True
    return False


def _looks_like_service_task_text(text: str) -> bool:
    service_terms = (
        "sistema",
        "automatic",
        "automatica",
        "automatico",
        "api",
        "web service",
        "servicio",
        "erp",
        "sap",
        "bot",
        "script",
        "genera",
        "generar",
        "calcula",
        "calcular",
        "sincroniza",
        "sincronizar",
        "actualiza",
        "actualizar",
        "notifica",
        "notificar",
        "procesa",
        "procesar",
        "ejecuta",
        "ejecutar",
    )
    return any(term in text for term in service_terms)


def _looks_like_user_task_text(text: str) -> bool:
    user_terms = (
        "aprueba",
        "aprobar",
        "autoriza",
        "autorizar",
        "firma",
        "firmar",
        "revisa",
        "revisar",
        "valida",
        "validar",
        "registra",
        "registrar",
        "ingresa",
        "ingresar",
        "solicita",
        "solicitar",
        "completa",
        "completar",
        "adjunta",
        "adjuntar",
    )
    return any(term in text for term in user_terms)


def _looks_like_data_object_text(text: str) -> bool:
    data_terms = (
        "acta",
        "reporte",
        "reportes",
        "pdf",
        "correo",
        "email",
        "solicitud",
        "guia",
        "guia de remision",
        "formato",
        "documento",
        "documentos",
        "informacion",
        "archivo",
        "anexo",
    )
    return any(term in text for term in data_terms)


def _looks_like_data_store(node: DiagramNode, text: str) -> bool:
    store_terms = (
        "base de datos",
        "bd",
        "repositorio",
        "almacen",
        "deposito",
        "archivo maestro",
        "maestro",
    )
    aspect_ratio = node.width / max(node.height, 1.0)
    cylinder_like = 0.7 <= aspect_ratio <= 1.8 and node.width >= 55 and node.height >= 40
    has_store_text = any(term in text for term in store_terms)
    return cylinder_like and has_store_text


def _looks_like_data_store_from_visuals(text: str) -> bool:
    if not text:
        return False
    store_terms = (
        "voucher",
        "obligacion",
        "archivo maestro",
        "maestro",
        "repositorio",
        "almacen",
        "deposito",
        "base de datos",
    )
    return any(term in text for term in store_terms)


def _looks_like_annotation_text(text: str, node: DiagramNode, in_primary_row: bool = False) -> bool:
    if in_primary_row:
        return False
    bullet_like = bool(re.search(r"(?:^| )[-*][^ ]", text))
    long_note = len(text) > 80 or text.count(",") >= 2
    slender = node.width < node.height * 1.4 or node.height > 120
    note_like = any(token in text for token in ("nota", "observacion", "consideracion", "importante"))
    short_action = len(text.split()) <= 8 and not bullet_like
    if short_action and node.width >= max(110.0, node.height * 1.25):
        return False
    return bullet_like or note_like or (long_note and slender)


def _looks_like_note_box(text: str, node: DiagramNode) -> bool:
    if node.width > 220 or node.height > 140:
        return False
    return any(token in text for token in ("una o mas", "uno o mas", "seleccion de"))


def _looks_like_subprocess(node: DiagramNode, text: str) -> bool:
    lines = text.count(" / ") + text.count(" - ") + max(text.count("  "), 0)
    multiword = len(text.split()) >= 4
    large = node.width >= 140 and node.height >= 70
    mentions = any(term in text for term in ("subproceso", "proceso", "flujo de", "gestion de", "proceso de"))
    return large and (mentions or (multiword and lines >= 1))


def _has_collapsed_marker(node: DiagramNode) -> bool:
    metadata = node.metadata or {}
    for key in ("collapsed", "marker", "plus_marker", "has_plus_marker"):
        value = metadata.get(key)
        if value in (True, "true", "plus", "+"):
            return True
    return False


def _is_association_candidate(source: DiagramNode, target: DiagramNode, edge: DiagramEdge) -> bool:
    if source.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
        return True
    if target.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
        return True
    normalized = _normalize_text(edge.text)
    if normalized and any(token in normalized for token in ("nota", "doc", "document", "acta", "reporte")):
        return True
    return False


def _is_message_flow_candidate(source: DiagramNode, target: DiagramNode, edge: DiagramEdge) -> bool:
    if source.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
        return False
    if target.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
        return False
    normalized = _normalize_text(edge.text)
    if any(token in normalized for token in ("mensaje", "correo", "email", "notifica", "envia", "recibe")):
        return True
    return source.node_type not in {NodeType.START_EVENT, NodeType.END_EVENT} and target.node_type not in {
        NodeType.START_EVENT,
        NodeType.END_EVENT,
    }


def _find_primary_sequence_row(diagram: DiagramDocument) -> list[DiagramNode]:
    allowed = {
        NodeType.START_EVENT,
        NodeType.END_EVENT,
        NodeType.TASK,
        NodeType.USER_TASK,
        NodeType.SERVICE_TASK,
        NodeType.SUBPROCESS,
        NodeType.COLLAPSED_SUBPROCESS,
        NodeType.EXCLUSIVE_GATEWAY,
        NodeType.PARALLEL_GATEWAY,
        NodeType.INCLUSIVE_GATEWAY,
        NodeType.EVENT_BASED_GATEWAY,
    }
    candidates = [
        node
        for node in diagram.nodes
        if not node.deleted and node.node_type in allowed and not _looks_like_document_header_artifact(
            node,
            _normalize_text(node.text),
            diagram.image_width,
            diagram.image_height,
        )
    ]
    if not candidates:
        return []

    centers_y = sorted(node.center.y for node in candidates)
    anchor_y = centers_y[len(centers_y) // 2]
    tolerance = max(42.0, diagram.image_height * 0.08)
    row = [node for node in candidates if abs(node.center.y - anchor_y) <= tolerance]
    row.sort(key=lambda item: (item.center.x, item.center.y))
    return row


def _refine_gateway_type(node: DiagramNode, text: str) -> NodeType:
    if any(token in text for token in ("evento", "mensaje", "timer", "temporizador", "senal")):
        return NodeType.EVENT_BASED_GATEWAY
    if any(token in text for token in ("paralelo", "simultaneo", "simultaneamente", "en paralelo")):
        return NodeType.PARALLEL_GATEWAY
    if any(token in text for token in ("uno o mas", "al menos uno", "cualquiera de")):
        return NodeType.INCLUSIVE_GATEWAY
    if any(token in text for token in ("si", "no", "aprobado", "rechazado", "decision", "aprueba")):
        return NodeType.EXCLUSIVE_GATEWAY
    if node.width == node.height:
        return NodeType.EXCLUSIVE_GATEWAY
    return node.node_type


def _find_ancestor_container(
    node: DiagramNode,
    nodes_by_id: dict[str, DiagramNode],
    expected_type: NodeType,
) -> DiagramNode | None:
    current = node
    visited: set[str] = set()
    while current.parent_id and current.parent_id not in visited:
        visited.add(current.parent_id)
        parent = nodes_by_id.get(current.parent_id)
        if parent is None:
            return None
        if parent.node_type == expected_type:
            return parent
        current = parent
    return None


def _node_contains_node(container: DiagramNode, node: DiagramNode) -> bool:
    return (
        container.id != node.id
        and container.x <= node.x
        and container.y <= node.y
        and container.x + container.width >= node.x + node.width
        and container.y + container.height >= node.y + node.height
    )


def _node_contains_bounds(container: DiagramNode, x: float, y: float, width: float, height: float) -> bool:
    return (
        container.x <= x
        and container.y <= y
        and container.x + container.width >= x + width
        and container.y + container.height >= y + height
    )


def _node_contains_point(container: DiagramNode, x: float, y: float) -> bool:
    return (
        container.x <= x <= container.x + container.width
        and container.y <= y <= container.y + container.height
    )


def _intersection_area(
    x1: float,
    y1: float,
    w1: float,
    h1: float,
    x2: float,
    y2: float,
    w2: float,
    h2: float,
) -> float:
    left = max(x1, x2)
    top = max(y1, y2)
    right = min(x1 + w1, x2 + w2)
    bottom = min(y1 + h1, y2 + h2)
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top)


def _detect_connectors(
    image_path: Path,
    nodes: list[DiagramNode],
    sketch_mode: bool = False,
) -> list[DiagramEdge]:
    if len(nodes) < 2:
        return []
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para detectar conectores.") from exc

    image = cv2_imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"No se pudo abrir la imagen rasterizada: {image_path}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges_image = cv2.Canny(gray, 35 if sketch_mode else 50, 120 if sketch_mode else 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges_image,
        rho=1,
        theta=math.pi / 180,
        threshold=55 if sketch_mode else 80,
        minLineLength=26 if sketch_mode else 40,
        maxLineGap=36 if sketch_mode else 20,
    )
    if lines is None:
        return []

    edge_map: dict[tuple[str, str], DiagramEdge] = {}
    for line in lines[:, 0]:
        x1, y1, x2, y2 = [float(value) for value in line]
        source = _nearest_node(nodes, x1, y1)
        target = _nearest_node(nodes, x2, y2)
        if not source or not target or source.id == target.id:
            continue
        ordered_source, ordered_target = _orient_edge(source, target)
        key = (ordered_source.id, ordered_target.id)
        candidate = edge_map.get(key)
        waypoint_data = [Point(x=x1, y=y1), Point(x=x2, y=y2)]
        if candidate is None:
            edge_map[key] = DiagramEdge(
                id=f"edge-{uuid.uuid4().hex[:8]}",
                edge_type=EdgeType.SEQUENCE_FLOW,
                source_id=ordered_source.id,
                target_id=ordered_target.id,
                confidence=0.35,
                waypoints=waypoint_data,
            )
        elif len(candidate.waypoints) < 4:
            candidate.waypoints.extend(waypoint_data)
            candidate.confidence = min(0.65, candidate.confidence + 0.05)
    return list(edge_map.values())


def _detect_data_store_candidates(
    image_path: Path,
    ocr_lines: list[OcrLine],
    image_height: int,
) -> list[DiagramNode]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para detectar depositos de datos.") from exc

    image = cv2_imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"No se pudo abrir la imagen rasterizada: {image_path}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        11,
    )
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[DiagramNode] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 12000:
            continue
        x, y, width, height = cv2.boundingRect(contour)
        if not (180 <= width <= 420 and 120 <= height <= 340):
            continue
        aspect_ratio = width / max(height, 1)
        if not (0.65 <= aspect_ratio <= 2.4):
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        fill_ratio = area / max(width * height, 1)
        if len(approx) < 6 or not (0.30 <= fill_ratio <= 0.98):
            continue

        text_lines = [
            line.text.strip()
            for line in ocr_lines
            if line.text.strip()
            and _intersection_area(x, y, width, height, line.x, line.y, line.width, line.height) > 0
        ]
        text = " ".join(dict.fromkeys(text_lines)).strip()
        normalized = _normalize_text(text)
        if y <= image_height * 0.22 and (
            not normalized
            or any(_is_global_document_header_text(line.text, line.y, image_height) for line in ocr_lines if _intersection_area(x, y, width, height, line.x, line.y, line.width, line.height) > 0)
        ):
            continue
        if text and not _looks_like_data_store_from_visuals(normalized):
            continue

        candidates.append(
            DiagramNode(
                id=f"node-{uuid.uuid4().hex[:8]}",
                node_type=NodeType.DATA_STORE,
                x=float(x),
                y=float(y),
                width=float(width),
                height=float(height),
                text=text,
                confidence=0.72 if text else 0.62,
                metadata={"detector": "data_store_contour"},
            )
        )

    return _dedupe_data_store_candidates(candidates)


def _detect_collapsed_marker(image_path: Path, node: DiagramNode) -> bool:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para detectar marcadores de subprocess colapsado.") from exc

    if node.width < 90 or node.height < 45:
        return False

    image = cv2_imread(image_path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return False

    marker_w = max(int(node.width * 0.24), 18)
    marker_h = max(int(node.height * 0.22), 14)
    x1 = max(int(node.x + node.width / 2 - marker_w / 2), 0)
    y1 = max(int(node.y + node.height - marker_h - 6), 0)
    x2 = min(x1 + marker_w, image.shape[1])
    y2 = min(y1 + marker_h, image.shape[0])
    if x2 - x1 < 10 or y2 - y1 < 8:
        return False

    roi = image[y1:y2, x1:x2]
    binary = cv2.adaptiveThreshold(
        roi,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        21,
        5,
    )
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max((x2 - x1) // 3, 7), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max((y2 - y1) // 2, 5)))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)
    crossing = cv2.bitwise_and(horizontal, vertical)
    return cv2.countNonZero(horizontal) > 8 and cv2.countNonZero(vertical) > 8 and cv2.countNonZero(crossing) > 0


def _detect_boundary_event_candidates(
    image_path: Path,
    nodes: list[DiagramNode],
) -> list[tuple[DiagramNode, DiagramNode]]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV es requerido para detectar boundary events.") from exc

    image = cv2_imread(image_path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return []
    blurred = cv2.GaussianBlur(image, (7, 7), 0)
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=18,
        param1=80,
        param2=18,
        minRadius=8,
        maxRadius=28,
    )
    if circles is None:
        return []

    activity_nodes = [
        node
        for node in nodes
        if not node.deleted and node.node_type in {
            NodeType.TASK,
            NodeType.USER_TASK,
            NodeType.SERVICE_TASK,
            NodeType.SUBPROCESS,
            NodeType.COLLAPSED_SUBPROCESS,
        }
    ]
    candidates: list[tuple[DiagramNode, DiagramNode]] = []
    for circle in circles[0]:
        cx, cy, radius = [float(value) for value in circle]
        attached_to = _find_boundary_attachment(activity_nodes, cx, cy, radius)
        if attached_to is None:
            continue
        attached_side = _boundary_attachment_side(attached_to, cx, cy, radius)
        if attached_side is None:
            continue
        candidates.append(
            (
                DiagramNode(
                    id=f"node-{uuid.uuid4().hex[:8]}",
                    node_type=NodeType.BOUNDARY_EVENT,
                    x=cx - radius,
                    y=cy - radius,
                    width=radius * 2.0,
                    height=radius * 2.0,
                    confidence=0.58,
                    metadata={"detector": "boundary_event_circle", "attached_side": attached_side},
                ),
                attached_to,
            )
        )
    return _dedupe_boundary_event_candidates(candidates)


def _nearest_node(nodes: list[DiagramNode], x: float, y: float) -> DiagramNode | None:
    best_node: DiagramNode | None = None
    best_distance = 999999.0
    for node in nodes:
        center = node.center
        distance = math.dist((x, y), (center.x, center.y))
        tolerance = max(node.width, node.height) * 1.2
        if distance < tolerance and distance < best_distance:
            best_distance = distance
            best_node = node
    return best_node


def _closest_connection_points(left: DiagramNode, right: DiagramNode) -> tuple[Point, Point]:
    left_center = left.center
    right_center = right.center
    return (
        Point(
            x=min(max(right_center.x, left.x), left.x + left.width),
            y=min(max(right_center.y, left.y), left.y + left.height),
        ),
        Point(
            x=min(max(left_center.x, right.x), right.x + right.width),
            y=min(max(left_center.y, right.y), right.y + right.height),
        ),
    )


def _shares_business_context(
    left: DiagramNode,
    right: DiagramNode,
    nodes_by_id: dict[str, DiagramNode],
) -> bool:
    left_pool = _find_ancestor_container(left, nodes_by_id, NodeType.POOL)
    right_pool = _find_ancestor_container(right, nodes_by_id, NodeType.POOL)
    if left_pool and right_pool:
        return left_pool.id == right_pool.id
    return True


def _axis_aligned_gap(left: DiagramNode, right: DiagramNode) -> float:
    horizontal_gap = max(left.x - (right.x + right.width), right.x - (left.x + left.width), 0.0)
    vertical_gap = max(left.y - (right.y + right.height), right.y - (left.y + left.height), 0.0)
    if horizontal_gap <= 0.0 or vertical_gap <= 0.0:
        return max(horizontal_gap, vertical_gap)
    return math.hypot(horizontal_gap, vertical_gap)


def _find_boundary_attachment(
    activities: list[DiagramNode],
    cx: float,
    cy: float,
    radius: float,
) -> DiagramNode | None:
    best: DiagramNode | None = None
    best_gap = float("inf")
    for activity in activities:
        gap = _boundary_attachment_gap(activity, cx, cy, radius)
        if gap is None:
            continue
        if gap < best_gap:
            best_gap = gap
            best = activity
    return best


def _boundary_attachment_gap(activity: DiagramNode, cx: float, cy: float, radius: float) -> float | None:
    expanded_left = activity.x - radius * 1.2
    expanded_top = activity.y - radius * 1.2
    expanded_right = activity.x + activity.width + radius * 1.2
    expanded_bottom = activity.y + activity.height + radius * 1.2
    if not (expanded_left <= cx <= expanded_right and expanded_top <= cy <= expanded_bottom):
        return None

    horizontal_gap = min(abs(cx - activity.x), abs(cx - (activity.x + activity.width)))
    vertical_gap = min(abs(cy - activity.y), abs(cy - (activity.y + activity.height)))
    edge_gap = min(horizontal_gap, vertical_gap)
    if edge_gap > radius * 1.4 + 6.0:
        return None
    inside_x = activity.x <= cx <= activity.x + activity.width
    inside_y = activity.y <= cy <= activity.y + activity.height
    if not (inside_x or inside_y):
        return None
    side = _boundary_attachment_side(activity, cx, cy, radius)
    if side is None:
        return None
    return edge_gap


def _looks_attached_to_boundary(boundary: DiagramNode, activity: DiagramNode) -> bool:
    radius = max(boundary.width, boundary.height) / 2.0
    return _boundary_attachment_side(activity, boundary.center.x, boundary.center.y, radius) is not None


def _boundary_attachment_side(activity: DiagramNode, cx: float, cy: float, radius: float) -> str | None:
    edge_tolerance = radius * 1.4 + 6.0
    corner_margin = max(radius * 1.6, min(activity.width, activity.height) * 0.18, 14.0)
    side_mid_tolerance_y = max(radius * 1.0, activity.height * 0.16, 12.0)
    side_mid_tolerance_x = max(radius * 1.0, activity.width * 0.16, 12.0)
    center_x = activity.center.x
    center_y = activity.center.y

    left_gap = abs(cx - activity.x)
    right_gap = abs(cx - (activity.x + activity.width))
    top_gap = abs(cy - activity.y)
    bottom_gap = abs(cy - (activity.y + activity.height))

    candidate_sides: list[tuple[str, float]] = []
    if (
        left_gap <= edge_tolerance
        and (activity.y + corner_margin) <= cy <= (activity.y + activity.height - corner_margin)
        and abs(cy - center_y) <= side_mid_tolerance_y
    ):
        candidate_sides.append(("left", left_gap))
    if (
        right_gap <= edge_tolerance
        and (activity.y + corner_margin) <= cy <= (activity.y + activity.height - corner_margin)
        and abs(cy - center_y) <= side_mid_tolerance_y
    ):
        candidate_sides.append(("right", right_gap))
    if (
        top_gap <= edge_tolerance
        and (activity.x + corner_margin) <= cx <= (activity.x + activity.width - corner_margin)
        and abs(cx - center_x) <= side_mid_tolerance_x
    ):
        candidate_sides.append(("top", top_gap))
    if (
        bottom_gap <= edge_tolerance
        and (activity.x + corner_margin) <= cx <= (activity.x + activity.width - corner_margin)
        and abs(cx - center_x) <= side_mid_tolerance_x
    ):
        candidate_sides.append(("bottom", bottom_gap))
    if not candidate_sides:
        return None
    candidate_sides.sort(key=lambda item: item[1])
    return candidate_sides[0][0]


def _node_overlap_ratio(node: DiagramNode, other: DiagramNode) -> float:
    intersection = _intersection_area(
        node.x,
        node.y,
        node.width,
        node.height,
        other.x,
        other.y,
        other.width,
        other.height,
    )
    if intersection <= 0:
        return 0.0
    smaller = min(node.width * node.height, other.width * other.height)
    return intersection / max(smaller, 1.0)


def _dedupe_data_store_candidates(nodes: list[DiagramNode]) -> list[DiagramNode]:
    deduped: list[DiagramNode] = []
    ranked = sorted(
        nodes,
        key=lambda item: (
            1 if item.text.strip() else 0,
            item.width * item.height,
            item.confidence,
        ),
        reverse=True,
    )
    for candidate in ranked:
        if any(_node_overlap_ratio(candidate, existing) > 0.55 for existing in deduped):
            continue
        deduped.append(candidate)
    return list(reversed(deduped))


def _dedupe_boundary_event_candidates(
    candidates: list[tuple[DiagramNode, DiagramNode]]
) -> list[tuple[DiagramNode, DiagramNode]]:
    deduped: list[tuple[DiagramNode, DiagramNode]] = []
    occupied_sides: set[tuple[str, str]] = set()
    for candidate, attached_to in sorted(
        candidates,
        key=lambda item: (item[1].id, item[0].confidence, item[0].width * item[0].height),
        reverse=True,
    ):
        side = str((candidate.metadata or {}).get("attached_side") or "")
        if side:
            side_key = (attached_to.id, side)
            if side_key in occupied_sides:
                continue
        if any(
            attached_to.id == existing_attached.id
            and _node_overlap_ratio(candidate, existing_candidate) > 0.5
            for existing_candidate, existing_attached in deduped
        ):
            continue
        deduped.append((candidate, attached_to))
        if side:
            occupied_sides.add((attached_to.id, side))
    return list(reversed(deduped))


def _orient_edge(source: DiagramNode, target: DiagramNode) -> tuple[DiagramNode, DiagramNode]:
    dx = target.center.x - source.center.x
    dy = target.center.y - source.center.y
    if abs(dx) >= abs(dy):
        return (source, target) if dx >= 0 else (target, source)
    return (source, target) if dy >= 0 else (target, source)


def _iou(left: DiagramNode, right: DiagramNode) -> float:
    x1 = max(left.x, right.x)
    y1 = max(left.y, right.y)
    x2 = min(left.x + left.width, right.x + right.width)
    y2 = min(left.y + left.height, right.y + right.height)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    intersection = (x2 - x1) * (y2 - y1)
    left_area = left.width * left.height
    right_area = right.width * right.height
    return intersection / max(left_area + right_area - intersection, 1.0)


def _coerce_node_type(value: str | None) -> NodeType:
    if not value:
        return NodeType.TASK
    normalized = value.strip().lower()
    for node_type in NodeType:
        if node_type.value == normalized:
            return node_type
    aliases = {
        "gateway": NodeType.EXCLUSIVE_GATEWAY,
        "evento_inicio": NodeType.START_EVENT,
        "evento_fin": NodeType.END_EVENT,
        "evento_intermedio": NodeType.INTERMEDIATE_EVENT,
        "sub_process": NodeType.SUBPROCESS,
        "datastorereference": NodeType.DATA_STORE,
        "data_store_reference": NodeType.DATA_STORE,
        "data store": NodeType.DATA_STORE,
    }
    return aliases.get(normalized, NodeType.TASK)


def _coerce_edge_type(value: str | None) -> EdgeType:
    if not value:
        return EdgeType.SEQUENCE_FLOW
    normalized = value.strip().lower()
    for edge_type in EdgeType:
        if edge_type.value == normalized:
            return edge_type
    return EdgeType.SEQUENCE_FLOW
