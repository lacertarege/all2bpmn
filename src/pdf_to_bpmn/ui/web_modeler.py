from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import (
    DiagramDocument,
    DiagramEdge,
    DiagramNode,
    EdgeType,
    NodeType,
    Point,
    normalize_event_nodes,
)
from pdf_to_bpmn.services.bpmn_semantic import BPMNSemanticExporter, parse_bpmn_semantics

try:
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEB_MODELER_AVAILABLE = True
except ImportError:  # pragma: no cover - optional runtime dependency
    QWebChannel = None
    QWebEngineSettings = None
    QWebEngineView = None
    WEB_MODELER_AVAILABLE = False


class WebModelerBridge(QObject):
    ready = Signal()
    status_changed = Signal(str)
    xml_received = Signal(str)

    @Slot()
    def notifyReady(self) -> None:
        self.ready.emit()

    @Slot(str)
    def reportStatus(self, message: str) -> None:
        self.status_changed.emit(message.strip())

    @Slot(str)
    def deliverXml(self, xml: str) -> None:
        self.xml_received.emit(xml)


class BpmnWebModelerWidget(QWidget):
    imported_diagram = Signal(object)
    focus_requested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.exporter = BPMNSemanticExporter(settings)
        self.diagram: DiagramDocument | None = None
        self._editor_ready = False
        self._pending_xml: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        self.sync_button = QPushButton("Sincronizar desde canvas")
        self.apply_button = QPushButton("Aplicar cambios del modelador")
        self.focus_button = QPushButton("Pantalla completa")
        self.status_label = QLabel("Modelador BPMN no inicializado")
        self.status_label.setWordWrap(True)
        toolbar.addWidget(self.sync_button)
        toolbar.addWidget(self.apply_button)
        toolbar.addWidget(self.focus_button)
        toolbar.addWidget(self.status_label, 1)
        layout.addLayout(toolbar)

        self.sync_button.clicked.connect(self.sync_from_diagram)
        self.apply_button.clicked.connect(self.request_import)
        self.focus_button.clicked.connect(self.focus_requested.emit)

        if not WEB_MODELER_AVAILABLE:
            self.sync_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.focus_button.setEnabled(False)
            layout.addWidget(
                QLabel(
                    "Qt WebEngine no esta disponible en este entorno. "
                    "El spike de bpmn-js requiere ese modulo para mostrarse dentro de la app."
                )
            )
            return

        self.bridge = WebModelerBridge()
        self.bridge.ready.connect(self._on_editor_ready)
        self.bridge.status_changed.connect(self.status_label.setText)
        self.bridge.xml_received.connect(self._on_xml_received)

        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)

        self.view = QWebEngineView(self)
        self.view.page().setWebChannel(self.channel)
        self.view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        self.view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        self.view.loadFinished.connect(self._on_page_loaded)
        self.view.load(QUrl.fromLocalFile(str(self._html_path())))
        layout.addWidget(self.view, 1)

    def set_diagram(self, diagram: DiagramDocument) -> None:
        self.diagram = diagram
        if WEB_MODELER_AVAILABLE:
            self.status_label.setText(
                "Diagrama listo para sincronizar con el modelador BPMN."
            )

    def mark_dirty(self) -> None:
        if WEB_MODELER_AVAILABLE and self.diagram is not None:
            self.status_label.setText(
                "Canvas Qt modificado. Sincroniza para reflejar esos cambios en bpmn-js."
            )

    def sync_from_diagram(self) -> None:
        if not WEB_MODELER_AVAILABLE or self.diagram is None:
            return
        temp_dir = self.settings.data_dir / "ui_cache"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_bpmn = temp_dir / "web_modeler_sync.bpmn"
        self.exporter.export(self.diagram, temp_bpmn)
        xml = temp_bpmn.read_text(encoding="utf-8")
        if not self._editor_ready:
            self._pending_xml = xml
            self.status_label.setText(
                "Esperando a que cargue bpmn-js para sincronizar el diagrama."
            )
            return
        self._load_xml_into_editor(xml)

    def request_import(self) -> None:
        if not WEB_MODELER_AVAILABLE:
            return
        if not self._editor_ready:
            QMessageBox.warning(
                self,
                "Modelador BPMN",
                "El editor web todavia no termino de inicializarse.",
            )
            return
        self.status_label.setText("Extrayendo XML desde bpmn-js...")
        self.view.page().runJavaScript("window.codexBpmn.exportXml();")

    def _on_page_loaded(self, ok: bool) -> None:
        if not ok:
            self.status_label.setText("No se pudo cargar la vista HTML del modelador BPMN.")

    def _on_editor_ready(self) -> None:
        self._editor_ready = True
        self.status_label.setText("Modelador BPMN listo.")
        if self._pending_xml:
            xml = self._pending_xml
            self._pending_xml = None
            self._load_xml_into_editor(xml)

    def _load_xml_into_editor(self, xml: str) -> None:
        encoded = json.dumps(xml)
        self.status_label.setText("Sincronizando BPMN con el editor web...")
        self.view.page().runJavaScript(f"window.codexBpmn.loadXml({encoded});")

    def _on_xml_received(self, xml: str) -> None:
        if not self.diagram:
            return
        try:
            temp_dir = self.settings.data_dir / "ui_cache"
            temp_dir.mkdir(parents=True, exist_ok=True)
            imported_path = temp_dir / "web_modeler_import.bpmn"
            imported_path.write_text(xml, encoding="utf-8")
            parsed = parse_bpmn_semantics(imported_path)
            imported = _diagram_from_parsed(parsed, self.diagram)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Error importando BPMN",
                f"No se pudo aplicar el XML generado por bpmn-js.\n\n{exc}",
            )
            self.status_label.setText("Fallo al reimportar el BPMN editado.")
            return

        self.status_label.setText(
            "Cambios del modelador aplicados al dominio local. Las incidencias se reiniciaron."
        )
        self.imported_diagram.emit(imported)

    def _html_path(self) -> Path:
        return Path(__file__).with_name("assets") / "bpmn_editor.html"


def _diagram_from_parsed(parsed: dict, previous: DiagramDocument) -> DiagramDocument:
    nodes = [
        DiagramNode(
            id=str(node["id"]),
            node_type=_coerce_node_type(node["type"]),
            x=float(node.get("x", 0.0)),
            y=float(node.get("y", 0.0)),
            width=float(node.get("width", 120.0)),
            height=float(node.get("height", 80.0)),
            text=str(node.get("text", "")),
            parent_id=node.get("parent_id"),
        )
        for node in parsed.get("nodes", [])
    ]
    normalize_event_nodes(nodes)
    edges = [
        DiagramEdge(
            id=str(edge["id"]),
            edge_type=_coerce_edge_type(edge["type"]),
            source_id=str(edge.get("source_id", "")),
            target_id=str(edge.get("target_id", "")),
            text=str(edge.get("text", "")),
            waypoints=[
                Point(x=float(point.get("x", 0.0)), y=float(point.get("y", 0.0)))
                for point in edge.get("waypoints", [])
            ],
        )
        for edge in parsed.get("edges", [])
    ]
    metadata = dict(previous.metadata)
    metadata["edited_with_web_modeler"] = True
    return DiagramDocument(
        source_pdf=previous.source_pdf,
        source_image=previous.source_image,
        image_width=int(parsed.get("image_width", previous.image_width)),
        image_height=int(parsed.get("image_height", previous.image_height)),
        nodes=nodes,
        edges=edges,
        issues=[],
        metadata=metadata,
    )


def _coerce_node_type(value: object) -> NodeType:
    if isinstance(value, NodeType):
        return value
    text = str(value or "").strip().lower()
    for node_type in NodeType:
        if node_type.value == text:
            return node_type
    if text == "datastorereference":
        return NodeType.DATA_STORE
    return NodeType.TASK


def _coerce_edge_type(value: object) -> EdgeType:
    if isinstance(value, EdgeType):
        return value
    text = str(value or "").strip().lower()
    for edge_type in EdgeType:
        if edge_type.value == text:
            return edge_type
    return EdgeType.SEQUENCE_FLOW
