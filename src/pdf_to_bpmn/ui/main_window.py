from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass

from PySide6.QtCore import QObject, QSettings, QThread, Qt, Signal
from PySide6.QtGui import QAction, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QGraphicsPixmapItem,
    QGraphicsScene,
)

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import (
    DiagramDocument,
    EdgeType,
    IssueSeverity,
    NodeType,
    ReviewIssue,
    normalize_event_node_size,
    normalize_event_nodes,
)
from pdf_to_bpmn.services.analysis import HybridDiagramAnalyzer, _coerce_edge_type, _coerce_node_type
from pdf_to_bpmn.services.bizagi_validation import BizagiStrictValidator
from pdf_to_bpmn.services.bpmn_semantic import BPMNSemanticExporter
from pdf_to_bpmn.services.rasterizer import SinglePagePdfRasterizer
from pdf_to_bpmn.services.storage import LocalWorkspaceStore, RunArtifacts
from pdf_to_bpmn.services.visio import VisioExporter
from pdf_to_bpmn.services.xpdl import XPDLExporter
from pdf_to_bpmn.ui.scene import DiagramScene, ZoomableGraphicsView
from pdf_to_bpmn.ui.web_modeler import BpmnWebModelerWidget


@dataclass
class LoadedDocument:
    artifacts: RunArtifacts
    diagram: DiagramDocument


class ExportWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        exporter: VisioExporter,
        bpmn_exporter: BPMNSemanticExporter,
        xpdl_exporter: XPDLExporter,
        diagram: DiagramDocument,
        bpmn_output_path: Path,
        visio_output_path: Path | None,
        xpdl_output_path: Path | None,
        export_kind: str,
        learning_dir_callback,
    ) -> None:
        super().__init__()
        self.exporter = exporter
        self.bpmn_exporter = bpmn_exporter
        self.xpdl_exporter = xpdl_exporter
        self.diagram = diagram
        self.bpmn_output_path = bpmn_output_path
        self.visio_output_path = visio_output_path
        self.xpdl_output_path = xpdl_output_path
        self.export_kind = export_kind
        self.learning_dir_callback = learning_dir_callback

    def run(self) -> None:
        try:
            if self.export_kind == "xpdl":
                if self.xpdl_output_path is None:
                    raise ValueError("Falta ruta de salida XPDL.")
                self.xpdl_exporter.export(self.diagram, self.xpdl_output_path)
            else:
                self.bpmn_exporter.export(self.diagram, self.bpmn_output_path)
            if self.visio_output_path is not None:
                self.exporter.export_from_bpmn(self.bpmn_output_path, self.visio_output_path, self.diagram)
            learning_dir = self.learning_dir_callback()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(
            {
                "learning_dir": learning_dir,
                "bpmn_output_path": self.bpmn_output_path,
                "visio_output_path": self.visio_output_path,
                "xpdl_output_path": self.xpdl_output_path,
                "export_kind": self.export_kind,
            }
        )


class BatchProcessWorker(QObject):
    progress = Signal(str, int, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        settings: Settings,
        store: LocalWorkspaceStore,
        input_paths: list[Path],
    ) -> None:
        super().__init__()
        self.settings = settings
        self.store = store
        self.input_paths = input_paths

    def run(self) -> None:
        results: list[LoadedDocument] = []
        try:
            rasterizer = SinglePagePdfRasterizer(self.settings.working_dpi)
            analyzer = HybridDiagramAnalyzer(self.settings)
            total = len(self.input_paths)
            for index, input_path in enumerate(self.input_paths, start=1):
                self.progress.emit(input_path.name, index, total)
                resolved_input = input_path.expanduser().resolve()
                if not resolved_input.exists():
                    raise FileNotFoundError(f"No existe el archivo de entrada: {resolved_input}")
                artifacts = self.store.create_run(resolved_input)
                rasterizer.rasterize(artifacts.source_pdf, artifacts.source_image)
                diagram = analyzer.analyze(artifacts.source_pdf, artifacts.source_image)
                self.store.save_diagram(diagram, artifacts.diagram_json)
                results.append(LoadedDocument(artifacts=artifacts, diagram=diagram))
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(results)


class ReprocessWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        settings: Settings,
        store: LocalWorkspaceStore,
        artifacts: RunArtifacts,
        current_diagram: DiagramDocument,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.store = store
        self.artifacts = artifacts
        self.current_diagram = current_diagram

    def run(self) -> None:
        try:
            rasterizer = SinglePagePdfRasterizer(self.settings.working_dpi)
            rasterizer.rasterize(self.artifacts.source_pdf, self.artifacts.source_image)
            analyzer = HybridDiagramAnalyzer(self.settings)
            refined = analyzer.refine(self.artifacts.source_pdf, self.artifacts.source_image, self.current_diagram)
            self.store.save_diagram(refined, self.artifacts.diagram_json)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(LoadedDocument(artifacts=self.artifacts, diagram=refined))


class ReviewMainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings,
        store: LocalWorkspaceStore,
        artifacts: RunArtifacts,
        diagram: DiagramDocument,
        exporter: VisioExporter,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.store = store
        self.artifacts = artifacts
        self.diagram = diagram
        self.exporter = exporter
        self.bpmn_exporter = BPMNSemanticExporter(settings)
        self.xpdl_exporter = XPDLExporter()
        self.bizagi_validator = BizagiStrictValidator()
        self.ui_settings = QSettings("pdf-to-bpmn", "pdf-to-bpmn-visio")
        self._export_thread: QThread | None = None
        self._export_worker: ExportWorker | None = None
        self._export_progress: QProgressDialog | None = None
        self._batch_thread: QThread | None = None
        self._batch_worker: BatchProcessWorker | None = None
        self._batch_progress: QProgressDialog | None = None
        self._reprocess_thread: QThread | None = None
        self._reprocess_worker: ReprocessWorker | None = None
        self._reprocess_progress: QProgressDialog | None = None
        self._updating_form = False
        self._current_kind: str | None = None
        self._current_id: str | None = None
        self._documents: list[LoadedDocument] = [LoadedDocument(artifacts=artifacts, diagram=diagram)]
        self._current_document_index = 0

        self.setWindowTitle("PDF a BPMN Visio")
        self.resize(1600, 900)
        self.setStatusBar(QStatusBar())
        self.diagram_title = str(diagram.metadata.get("title") or "Diagrama BPMN")

        self.scene = DiagramScene(diagram)
        self.scene.node_selected.connect(self._on_node_selected)
        self.scene.edge_selected.connect(self._on_edge_selected)
        self.scene.changed_model.connect(self._on_scene_changed)
        self._modeler_focus_mode = False

        self.source_view = self._build_source_view(diagram.source_image)
        self.diagram_view = ZoomableGraphicsView(auto_fit=True)
        self.diagram_view.setScene(self.scene)
        self.web_modeler = BpmnWebModelerWidget(self.settings)
        self.web_modeler.set_diagram(self.diagram)
        self.web_modeler.imported_diagram.connect(self._apply_imported_web_diagram)
        self.web_modeler.focus_requested.connect(self._toggle_modeler_focus_mode)

        self.issue_list = QListWidget()
        self.issue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.issue_list.itemSelectionChanged.connect(self._on_issue_selected)
        self.node_list = QListWidget()
        self.node_list.itemSelectionChanged.connect(self._on_node_list_selected)
        self.edge_list = QListWidget()
        self.edge_list.itemSelectionChanged.connect(self._on_edge_list_selected)

        self.selection_title = QLabel("Sin seleccion")
        self.node_group = self._build_node_form()
        self.edge_group = self._build_edge_form()
        self.document_selector = QComboBox()
        self.document_selector.currentIndexChanged.connect(self._on_document_selected)
        self.processing_label = QLabel(f"Documento activo: {self._document_label(artifacts)}")
        self.credits_link = QLabel('<a href="#">Creditos</a>')
        self.credits_link.setTextFormat(Qt.TextFormat.RichText)
        self.credits_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.credits_link.setOpenExternalLinks(False)
        self.credits_link.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.credits_link.setStyleSheet("color: #6b7280; font-size: 10px;")
        self.credits_link.linkActivated.connect(self._show_credits)

        side_tabs = QTabWidget()
        side_tabs.addTab(self._build_selection_tab(), "Seleccion")
        side_tabs.addTab(self._build_issues_tab(), "Problemas")
        side_tabs.addTab(self._build_elements_tab(), "Elementos")

        self.export_bizagi_button = QPushButton("Exportar BPMN Bizagi")
        self.export_bizagi_button.clicked.connect(self._export_bizagi)
        self.export_xpdl_button = QPushButton("Exportar XPDL Bizagi")
        self.export_xpdl_button.clicked.connect(self._export_xpdl)
        self.export_visio_button = QPushButton("Exportar VSDX Visio")
        self.export_visio_button.clicked.connect(self._export_visio)
        self.reprocess_button = QPushButton("Reprocesar")
        self.reprocess_button.clicked.connect(self._reprocess_current_input)
        self.next_button = QPushButton("Siguiente archivo")
        self.next_button.clicked.connect(self._go_to_next_document)

        side_panel = QWidget()
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(QLabel("Archivos cargados"))
        side_layout.addWidget(self.document_selector)
        side_layout.addWidget(self.processing_label)
        side_layout.addWidget(self.credits_link)
        side_layout.addWidget(side_tabs)
        side_layout.addWidget(self.export_bizagi_button)
        side_layout.addWidget(self.export_xpdl_button)
        side_layout.addWidget(self.export_visio_button)
        side_layout.addWidget(self.reprocess_button)
        side_layout.addWidget(self.next_button)

        self.side_panel = side_panel
        self.top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.source_panel = self._wrap_panel("Archivo origen", self.source_view)
        self.reconstructed_panel = self._wrap_panel("BPMN reconstruido", self._build_reconstructed_panel())
        self.top_splitter.addWidget(self.source_panel)
        self.top_splitter.addWidget(self.reconstructed_panel)
        self.top_splitter.setStretchFactor(0, 1)
        self.top_splitter.setStretchFactor(1, 1)
        self.top_splitter.setSizes([780, 780])

        self.main_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_splitter.addWidget(self.top_splitter)
        self.main_splitter.addWidget(self.side_panel)
        self.main_splitter.setStretchFactor(0, 4)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([680, 220])
        self.setCentralWidget(self.main_splitter)

        self._build_toolbar()
        self._reload_document_selector()
        self._reload_lists()
        self._refresh_issue_list()
        self._set_selection(None, None)
        self.web_modeler.sync_from_diagram()

    def _build_source_view(self, image_path: Path) -> ZoomableGraphicsView:
        scene = QGraphicsScene()
        pixmap = self._load_source_pixmap(image_path)
        scene.addItem(QGraphicsPixmapItem(pixmap))
        scene.setSceneRect(pixmap.rect())
        view = ZoomableGraphicsView(auto_fit=True)
        view.setScene(scene)
        return view

    def _build_reconstructed_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.reconstructed_tabs = QTabWidget()
        self.reconstructed_tabs.addTab(self.diagram_view, "Canvas Qt")
        self.reconstructed_tabs.addTab(self.web_modeler, "Modelador BPMN")
        layout.addWidget(self.reconstructed_tabs)
        return container

    def _wrap_panel(self, title: str, widget: QWidget) -> QWidget:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.addWidget(widget)
        return box

    def _build_toolbar(self) -> None:
        open_action = QAction("Abrir archivo(s)", self)
        open_action.triggered.connect(self._open_inputs)
        self.addAction(open_action)

        save_action = QAction("Guardar borrador", self)
        save_action.triggered.connect(self._save_draft)
        self.addAction(save_action)

        resolve_action = QAction("Resolver problema", self)
        resolve_action.triggered.connect(self._resolve_selected_issue)
        self.addAction(resolve_action)

        export_bizagi_action = QAction("Exportar BPMN Bizagi", self)
        export_bizagi_action.triggered.connect(self._export_bizagi)
        self.addAction(export_bizagi_action)
        self.export_bizagi_action = export_bizagi_action

        export_xpdl_action = QAction("Exportar XPDL Bizagi", self)
        export_xpdl_action.triggered.connect(self._export_xpdl)
        self.addAction(export_xpdl_action)
        self.export_xpdl_action = export_xpdl_action

        export_visio_action = QAction("Exportar VSDX Visio", self)
        export_visio_action.triggered.connect(self._export_visio)
        self.addAction(export_visio_action)
        self.export_visio_action = export_visio_action

        reprocess_action = QAction("Reprocesar", self)
        reprocess_action.triggered.connect(self._reprocess_current_input)
        self.addAction(reprocess_action)
        self.reprocess_action = reprocess_action

        next_action = QAction("Siguiente archivo", self)
        next_action.triggered.connect(self._go_to_next_document)
        self.addAction(next_action)
        self.next_action = next_action

        self.modeler_focus_action = QAction("Modelador pantalla completa", self)
        self.modeler_focus_action.setCheckable(True)
        self.modeler_focus_action.triggered.connect(self._toggle_modeler_focus_mode)
        self.addAction(self.modeler_focus_action)

        toolbar = self.addToolBar("Principal")
        toolbar.addAction(open_action)
        toolbar.addAction(save_action)
        toolbar.addAction(resolve_action)
        toolbar.addAction(export_bizagi_action)
        toolbar.addAction(export_xpdl_action)
        toolbar.addAction(export_visio_action)
        toolbar.addAction(reprocess_action)
        toolbar.addAction(next_action)
        toolbar.addAction(self.modeler_focus_action)

    def _toggle_modeler_focus_mode(self, checked: bool | None = None) -> None:
        target_state = (not self._modeler_focus_mode) if checked is None else bool(checked)
        self._modeler_focus_mode = target_state
        if target_state:
            self.reconstructed_tabs.setCurrentWidget(self.web_modeler)
            self.source_panel.setVisible(False)
            self.side_panel.setVisible(False)
            self.showMaximized()
            self.statusBar().showMessage("Modelador BPMN en pantalla completa. Usa el mismo boton para restaurar.", 5000)
        else:
            self.source_panel.setVisible(True)
            self.side_panel.setVisible(True)
            self.top_splitter.setSizes([780, 780])
            self.main_splitter.setSizes([680, 220])
            self.statusBar().showMessage("Vista normal restaurada.", 3000)
        self.modeler_focus_action.blockSignals(True)
        self.modeler_focus_action.setChecked(target_state)
        self.modeler_focus_action.setText("Restaurar vista" if target_state else "Modelador pantalla completa")
        self.modeler_focus_action.blockSignals(False)

    def _build_selection_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.selection_title)
        layout.addWidget(self.node_group)
        layout.addWidget(self.edge_group)
        layout.addStretch(1)
        return container

    def _build_issues_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.issue_list)
        self.resolve_issues_button = QPushButton("Marcar seleccionados como resueltos")
        self.resolve_issues_button.clicked.connect(self._resolve_selected_issue)
        layout.addWidget(self.resolve_issues_button)
        return container

    def _build_elements_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(QLabel("Nodos"))
        layout.addWidget(self.node_list)
        layout.addWidget(QLabel("Conectores"))
        layout.addWidget(self.edge_list)
        return container

    def _build_node_form(self) -> QGroupBox:
        group = QGroupBox("Nodo")
        form = QFormLayout(group)

        self.node_type_combo = QComboBox()
        for node_type in NodeType:
            self.node_type_combo.addItem(node_type.value, node_type)
        self.node_type_combo.currentIndexChanged.connect(self._apply_node_form)

        self.node_text_edit = QTextEdit()
        self.node_text_edit.setFixedHeight(72)
        self.node_text_edit.textChanged.connect(self._apply_node_form)

        self.node_parent_combo = QComboBox()
        self.node_parent_combo.currentIndexChanged.connect(self._apply_node_form)

        self.node_x_spin = _coordinate_spinbox()
        self.node_y_spin = _coordinate_spinbox()
        self.node_w_spin = _coordinate_spinbox(minimum=5.0)
        self.node_h_spin = _coordinate_spinbox(minimum=5.0)
        for spinbox in (
            self.node_x_spin,
            self.node_y_spin,
            self.node_w_spin,
            self.node_h_spin,
        ):
            spinbox.valueChanged.connect(self._apply_node_form)

        self.node_deleted_check = QCheckBox("Marcado como falso positivo")
        self.node_deleted_check.stateChanged.connect(self._apply_node_form)

        form.addRow("Tipo BPMN", self.node_type_combo)
        form.addRow("Texto", self.node_text_edit)
        form.addRow("Contenedor", self.node_parent_combo)
        form.addRow("X", self.node_x_spin)
        form.addRow("Y", self.node_y_spin)
        form.addRow("Ancho", self.node_w_spin)
        form.addRow("Alto", self.node_h_spin)
        form.addRow("", self.node_deleted_check)
        return group

    def _build_edge_form(self) -> QGroupBox:
        group = QGroupBox("Conector")
        form = QFormLayout(group)

        self.edge_type_combo = QComboBox()
        for edge_type in EdgeType:
            self.edge_type_combo.addItem(edge_type.value, edge_type)
        self.edge_type_combo.currentIndexChanged.connect(self._apply_edge_form)

        self.edge_source_combo = QComboBox()
        self.edge_source_combo.currentIndexChanged.connect(self._apply_edge_form)
        self.edge_target_combo = QComboBox()
        self.edge_target_combo.currentIndexChanged.connect(self._apply_edge_form)

        self.edge_text_edit = QLineEdit()
        self.edge_text_edit.textChanged.connect(self._apply_edge_form)

        self.edge_deleted_check = QCheckBox("Marcado como falso positivo")
        self.edge_deleted_check.stateChanged.connect(self._apply_edge_form)

        form.addRow("Tipo BPMN", self.edge_type_combo)
        form.addRow("Origen", self.edge_source_combo)
        form.addRow("Destino", self.edge_target_combo)
        form.addRow("Texto", self.edge_text_edit)
        form.addRow("", self.edge_deleted_check)
        return group

    def _reload_lists(self) -> None:
        self.node_list.clear()
        self.edge_list.clear()

        for node in self.diagram.nodes:
            item = QListWidgetItem(
                f"{node.id} | {node.node_type.value} | {node.text or '(sin texto)'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, node.id)
            if node.deleted:
                item.setForeground(Qt.GlobalColor.darkGray)
            self.node_list.addItem(item)

        for edge in self.diagram.edges:
            item = QListWidgetItem(
                f"{edge.id} | {edge.edge_type.value} | {edge.source_id} -> {edge.target_id}"
            )
            item.setData(Qt.ItemDataRole.UserRole, edge.id)
            if edge.deleted:
                item.setForeground(Qt.GlobalColor.darkGray)
            self.edge_list.addItem(item)

        self._reload_combo_sources()

    def _reload_combo_sources(self) -> None:
        self.node_parent_combo.blockSignals(True)
        self.edge_source_combo.blockSignals(True)
        self.edge_target_combo.blockSignals(True)

        self.node_parent_combo.clear()
        self.node_parent_combo.addItem("(sin contenedor)", None)
        self.edge_source_combo.clear()
        self.edge_target_combo.clear()

        for node in self.diagram.nodes:
            self.edge_source_combo.addItem(node.id, node.id)
            self.edge_target_combo.addItem(node.id, node.id)
            if node.node_type in {NodeType.POOL, NodeType.LANE}:
                self.node_parent_combo.addItem(node.id, node.id)

        self.node_parent_combo.blockSignals(False)
        self.edge_source_combo.blockSignals(False)
        self.edge_target_combo.blockSignals(False)

    def _refresh_issue_list(self) -> None:
        self.issue_list.clear()
        for issue in self.diagram.issues:
            prefix = "[RESUELTO]" if issue.resolved else "[PENDIENTE]"
            item = QListWidgetItem(f"{prefix} {issue.severity.value.upper()} | {issue.message}")
            item.setData(Qt.ItemDataRole.UserRole, issue.id)
            if issue.resolved:
                item.setForeground(Qt.GlobalColor.darkGray)
            self.issue_list.addItem(item)
        bizagi_enabled = not self._blocking_issues("bizagi")
        xpdl_enabled = not self._blocking_issues("bizagi")
        visio_enabled = not self._blocking_issues("visio")
        self.export_bizagi_action.setEnabled(bizagi_enabled)
        self.export_xpdl_action.setEnabled(xpdl_enabled)
        self.export_visio_action.setEnabled(visio_enabled)
        self.export_bizagi_button.setEnabled(bizagi_enabled)
        self.export_xpdl_button.setEnabled(xpdl_enabled)
        self.export_visio_button.setEnabled(visio_enabled)
        has_next = self._current_document_index < len(self._documents) - 1
        self.next_action.setEnabled(has_next)
        self.next_button.setEnabled(has_next)

    def _set_selection(self, kind: str | None, entity_id: str | None) -> None:
        self._current_kind = kind
        self._current_id = entity_id
        self._updating_form = True
        try:
            self.node_group.setVisible(kind == "node")
            self.edge_group.setVisible(kind == "edge")
            if kind == "node" and entity_id:
                node = self.diagram.find_node(entity_id)
                if not node:
                    return
                self.selection_title.setText(f"Nodo seleccionado: {node.id}")
                self.node_type_combo.setCurrentIndex(self.node_type_combo.findData(node.node_type))
                self.node_text_edit.setPlainText(node.text)
                self.node_x_spin.setValue(node.x)
                self.node_y_spin.setValue(node.y)
                self.node_w_spin.setValue(node.width)
                self.node_h_spin.setValue(node.height)
                self.node_deleted_check.setChecked(node.deleted)
                parent_index = self.node_parent_combo.findData(node.parent_id)
                self.node_parent_combo.setCurrentIndex(max(parent_index, 0))
            elif kind == "edge" and entity_id:
                edge = self.diagram.find_edge(entity_id)
                if not edge:
                    return
                self.selection_title.setText(f"Conector seleccionado: {edge.id}")
                self.edge_type_combo.setCurrentIndex(self.edge_type_combo.findData(edge.edge_type))
                self.edge_source_combo.setCurrentIndex(self.edge_source_combo.findData(edge.source_id))
                self.edge_target_combo.setCurrentIndex(self.edge_target_combo.findData(edge.target_id))
                self.edge_text_edit.setText(edge.text)
                self.edge_deleted_check.setChecked(edge.deleted)
            else:
                self.selection_title.setText("Sin seleccion")
        finally:
            self._updating_form = False

    def _on_node_selected(self, node_id: str) -> None:
        self._select_list_item(self.node_list, node_id)
        self._set_selection("node", node_id)

    def _on_edge_selected(self, edge_id: str) -> None:
        self._select_list_item(self.edge_list, edge_id)
        self._set_selection("edge", edge_id)

    def _on_scene_changed(self) -> None:
        self._documents[self._current_document_index].diagram = self.diagram
        self._clear_profile_issues(BizagiStrictValidator.PROFILE)
        self._reload_lists()
        self._refresh_issue_list()
        self.store.save_diagram(self.diagram, self.artifacts.diagram_json)
        self.web_modeler.mark_dirty()

    def _on_node_list_selected(self) -> None:
        item = self.node_list.currentItem()
        if not item:
            return
        node_id = item.data(Qt.ItemDataRole.UserRole)
        self.scene.select_node(node_id)
        self._set_selection("node", node_id)

    def _on_edge_list_selected(self) -> None:
        item = self.edge_list.currentItem()
        if not item:
            return
        edge_id = item.data(Qt.ItemDataRole.UserRole)
        self.scene.select_edge(edge_id)
        self._set_selection("edge", edge_id)

    def _on_issue_selected(self) -> None:
        issues = self._selected_issues()
        if not issues:
            return
        if len(issues) > 1:
            self.selection_title.setText(f"{len(issues)} problemas seleccionados")
            return
        issue = issues[0]
        if issue.related_kind == "node" and issue.related_id:
            self.scene.select_node(issue.related_id)
            self._set_selection("node", issue.related_id)
        elif issue.related_kind == "edge" and issue.related_id:
            self.scene.select_edge(issue.related_id)
            self._set_selection("edge", issue.related_id)

    def _apply_node_form(self) -> None:
        if self._updating_form or self._current_kind != "node" or not self._current_id:
            return
        node = self.diagram.find_node(self._current_id)
        if not node:
            return
        node.node_type = _coerce_node_type(self.node_type_combo.currentData())
        node.text = self.node_text_edit.toPlainText().strip()
        node.parent_id = self.node_parent_combo.currentData()
        node.x = self.node_x_spin.value()
        node.y = self.node_y_spin.value()
        node.width = self.node_w_spin.value()
        node.height = self.node_h_spin.value()
        normalize_event_node_size(node)
        node.deleted = self.node_deleted_check.isChecked()
        self.scene.refresh_node(node.id)
        self._reload_lists()

    def _apply_edge_form(self) -> None:
        if self._updating_form or self._current_kind != "edge" or not self._current_id:
            return
        edge = self.diagram.find_edge(self._current_id)
        if not edge:
            return
        edge.edge_type = _coerce_edge_type(self.edge_type_combo.currentData())
        edge.source_id = self.edge_source_combo.currentData()
        edge.target_id = self.edge_target_combo.currentData()
        edge.text = self.edge_text_edit.text().strip()
        edge.deleted = self.edge_deleted_check.isChecked()
        self.scene.refresh_edge(edge.id)
        self._reload_lists()

    def _save_draft(self) -> None:
        self._documents[self._current_document_index].diagram = self.diagram
        self.store.save_diagram(self.diagram, self.artifacts.diagram_json)
        self.statusBar().showMessage(f"Borrador guardado en {self.artifacts.diagram_json}", 5000)

    def _open_inputs(self) -> None:
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "Seleccionar PDF(s) o imagen(es)",
            str(self._last_input_directory()),
            "Archivos compatibles (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)",
        )
        if not selected:
            return
        input_paths = [Path(item) for item in selected]
        self._remember_input_directory(input_paths[0])
        self._process_input_batch(input_paths)

    def _resolve_selected_issue(self) -> None:
        issues = self._selected_issues()
        if not issues:
            QMessageBox.information(self, "Problema", "Selecciona un problema primero.")
            return
        for issue in issues:
            issue.resolved = True
        self._refresh_issue_list()
        self.store.save_diagram(self.diagram, self.artifacts.diagram_json)

    def _selected_issue(self) -> ReviewIssue | None:
        issues = self._selected_issues()
        return issues[0] if issues else None

    def _selected_issues(self) -> list[ReviewIssue]:
        selected_ids = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.issue_list.selectedItems()
        }
        if not selected_ids:
            return []
        return [issue for issue in self.diagram.issues if issue.id in selected_ids]

    def _clear_profile_issues(self, profile: str) -> None:
        self.diagram.issues = [
            issue
            for issue in self.diagram.issues
            if (issue.metadata or {}).get("profile") != profile
        ]

    def _blocking_issues(self, export_kind: str | None = None) -> list[ReviewIssue]:
        ignored_profiles = set()
        if export_kind == "visio":
            ignored_profiles.add(BizagiStrictValidator.PROFILE)
        return [
            issue
            for issue in self.diagram.unresolved_issues()
            if issue.severity == IssueSeverity.ERROR
            and (issue.metadata or {}).get("profile") not in ignored_profiles
        ]

    def _export_bizagi(self) -> None:
        self.bizagi_validator.sync_issues(self.diagram)
        self._documents[self._current_document_index].diagram = self.diagram
        self._refresh_issue_list()
        self.store.save_diagram(self.diagram, self.artifacts.diagram_json)
        blocking = self._blocking_issues("bizagi")
        if blocking:
            QMessageBox.warning(
                self,
                "Exportacion bloqueada",
                "Debes resolver los errores pendientes de interoperabilidad Bizagi antes de exportar.",
            )
            return
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar BPMN para Bizagi",
            str(self._default_export_path("bizagi")),
            "BPMN (*.bpmn)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.lower() != ".bpmn":
            destination = destination.with_suffix(".bpmn")
        self.ui_settings.setValue("last_export_dir_bizagi", str(destination.parent))
        self._save_draft()
        self._start_export_job(destination, None, None, "bizagi")

    def _export_visio(self) -> None:
        blocking = self._blocking_issues("visio")
        if blocking:
            QMessageBox.warning(
                self,
                "Exportacion bloqueada",
                "Debes resolver los errores pendientes antes de exportar.",
            )
            return
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar VSDX para Visio",
            str(self._default_export_path("visio")),
            "Visio (*.vsdx)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.lower() != ".vsdx":
            destination = destination.with_suffix(".vsdx")
        self.ui_settings.setValue("last_export_dir_visio", str(destination.parent))
        self._save_draft()
        bpmn_destination = destination.with_suffix(".bpmn")
        self._start_export_job(bpmn_destination, destination, None, "visio")

    def _export_xpdl(self) -> None:
        self.bizagi_validator.sync_issues(self.diagram)
        self._documents[self._current_document_index].diagram = self.diagram
        self._refresh_issue_list()
        self.store.save_diagram(self.diagram, self.artifacts.diagram_json)
        blocking = self._blocking_issues("bizagi")
        if blocking:
            QMessageBox.warning(
                self,
                "Exportacion bloqueada",
                "Debes resolver los errores pendientes de interoperabilidad Bizagi antes de exportar.",
            )
            return
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar XPDL para Bizagi",
            str(self._default_export_path("xpdl")),
            "XPDL (*.xpdl)",
        )
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.lower() != ".xpdl":
            destination = destination.with_suffix(".xpdl")
        self.ui_settings.setValue("last_export_dir_xpdl", str(destination.parent))
        self._save_draft()
        self._start_export_job(destination.with_suffix(".bpmn"), None, destination, "xpdl")

    def _start_export_job(
        self,
        bpmn_output_path: Path,
        visio_output_path: Path | None,
        xpdl_output_path: Path | None,
        export_kind: str,
    ) -> None:
        self.export_bizagi_action.setEnabled(False)
        self.export_xpdl_action.setEnabled(False)
        self.export_visio_action.setEnabled(False)
        self.export_bizagi_button.setEnabled(False)
        self.export_xpdl_button.setEnabled(False)
        self.export_visio_button.setEnabled(False)
        if export_kind == "xpdl":
            export_label = "Exportando XPDL Bizagi..."
        elif visio_output_path is None:
            export_label = "Exportando BPMN Bizagi..."
        else:
            export_label = "Exportando a Visio..."
        self._export_progress = QProgressDialog(
            export_label, None, 0, 0, self
        )
        self._export_progress.setWindowTitle("Exportando")
        self._export_progress.setMinimumDuration(0)
        self._export_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._export_progress.setAutoClose(False)
        self._export_progress.setAutoReset(False)
        self._export_progress.show()

        self._export_thread = QThread(self)
        self._export_worker = ExportWorker(
            self.exporter,
            self.bpmn_exporter,
            self.xpdl_exporter,
            self.diagram,
            bpmn_output_path,
            visio_output_path,
            xpdl_output_path,
            export_kind,
            lambda: self.store.archive_learning_sample(self.artifacts, self.diagram),
        )
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.finished.connect(self._export_thread.quit)
        self._export_worker.failed.connect(self._export_thread.quit)
        self._export_thread.finished.connect(self._cleanup_export_job)
        self._export_thread.start()

    def _on_export_finished(self, result: object) -> None:
        if self._export_progress:
            self._export_progress.close()
        self._refresh_issue_list()
        payload = dict(result) if isinstance(result, dict) else {}
        learning_dir = payload.get("learning_dir")
        bpmn_output_path = payload.get("bpmn_output_path")
        visio_output_path = payload.get("visio_output_path")
        xpdl_output_path = payload.get("xpdl_output_path")
        if visio_output_path:
            message = (
                f"Archivo Visio:\n{visio_output_path}\n\n"
                f"BPMN auxiliar:\n{bpmn_output_path}\n\n"
                f"Aprendizaje guardado en:\n{learning_dir}"
            )
        elif xpdl_output_path:
            message = (
                f"Archivo XPDL para Bizagi:\n{xpdl_output_path}\n\n"
                f"Aprendizaje guardado en:\n{learning_dir}"
            )
        else:
            message = (
                f"Archivo BPMN para Bizagi:\n{bpmn_output_path}\n\n"
                f"Aprendizaje guardado en:\n{learning_dir}"
            )
        answer = QMessageBox.question(
            self,
            "Exportacion completada",
            message + "\n\n¿Deseas realizar otra exportacion del mismo diagrama?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("Puedes exportar nuevamente el mismo diagrama.", 5000)
            return
        self._advance_to_next_document(reset_when_finished=True)

    def _on_export_failed(self, message: str) -> None:
        if self._export_progress:
            self._export_progress.close()
        self._refresh_issue_list()
        QMessageBox.critical(self, "Error exportando", message)

    def _cleanup_export_job(self) -> None:
        if self._export_worker:
            self._export_worker.deleteLater()
        if self._export_thread:
            self._export_thread.deleteLater()
        self._export_worker = None
        self._export_thread = None
        self._export_progress = None

    def _process_input_batch(self, input_paths: list[Path]) -> None:
        self._save_draft()
        self._batch_progress = QProgressDialog("Preparando procesamiento...", None, 0, 0, self)
        self._batch_progress.setWindowTitle("Procesando archivo(s)")
        self._batch_progress.setMinimumDuration(0)
        self._batch_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._batch_progress.setAutoClose(False)
        self._batch_progress.setAutoReset(False)
        self._batch_progress.show()

        self._batch_thread = QThread(self)
        self._batch_worker = BatchProcessWorker(self.settings, self.store, input_paths)
        self._batch_worker.moveToThread(self._batch_thread)
        self._batch_thread.started.connect(self._batch_worker.run)
        self._batch_worker.progress.connect(self._on_batch_progress)
        self._batch_worker.finished.connect(self._on_batch_finished)
        self._batch_worker.failed.connect(self._on_batch_failed)
        self._batch_worker.finished.connect(self._batch_thread.quit)
        self._batch_worker.failed.connect(self._batch_thread.quit)
        self._batch_thread.finished.connect(self._cleanup_batch_job)
        self._batch_thread.start()

    def _reprocess_current_input(self) -> None:
        current_input = self.artifacts.source_pdf
        if not current_input.exists():
            QMessageBox.warning(self, "Reprocesar", "El archivo actual no esta disponible para reprocesar.")
            return
        self._save_draft()
        self._reprocess_progress = QProgressDialog(
            f"Reprocesando en profundidad...\n{current_input.name}",
            None,
            0,
            0,
            self,
        )
        self._reprocess_progress.setWindowTitle("Reprocesando")
        self._reprocess_progress.setMinimumDuration(0)
        self._reprocess_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._reprocess_progress.setAutoClose(False)
        self._reprocess_progress.setAutoReset(False)
        self._reprocess_progress.show()

        self._reprocess_thread = QThread(self)
        self._reprocess_worker = ReprocessWorker(
            self.settings,
            self.store,
            self.artifacts,
            self.diagram,
        )
        self._reprocess_worker.moveToThread(self._reprocess_thread)
        self._reprocess_thread.started.connect(self._reprocess_worker.run)
        self._reprocess_worker.finished.connect(self._on_reprocess_finished)
        self._reprocess_worker.failed.connect(self._on_reprocess_failed)
        self._reprocess_worker.finished.connect(self._reprocess_thread.quit)
        self._reprocess_worker.failed.connect(self._reprocess_thread.quit)
        self._reprocess_thread.finished.connect(self._cleanup_reprocess_job)
        self._reprocess_thread.start()

    def _on_batch_progress(self, file_name: str, index: int, total: int) -> None:
        if self._batch_progress:
            self._batch_progress.setLabelText(f"Procesando archivo {index}/{total}...\n{file_name}")
        self.processing_label.setText(f"Procesando ahora: {file_name}")

    def _on_batch_finished(self, results: object) -> None:
        if self._batch_progress:
            self._batch_progress.close()
        loaded = list(results) if isinstance(results, list) else []
        if not loaded:
            self.processing_label.setText("Documento activo: sin documentos cargados")
            return
        if len(loaded) == 1 and loaded[0].artifacts.source_pdf.name == self.artifacts.source_pdf.name:
            self._documents[self._current_document_index] = loaded[0]
            self._reload_document_selector()
            self.document_selector.setCurrentIndex(self._current_document_index)
            self._load_document(loaded[0].artifacts, loaded[0].diagram)
            self.statusBar().showMessage(f"Se reproceso {loaded[0].artifacts.source_pdf.name}.", 5000)
            return
        self._documents.extend(loaded)
        self._reload_document_selector()
        self.document_selector.setCurrentIndex(len(self._documents) - len(loaded))
        self.statusBar().showMessage(f"Se procesaron {len(loaded)} archivo(s).", 5000)

    def _on_batch_failed(self, message: str) -> None:
        if self._batch_progress:
            self._batch_progress.close()
        self.processing_label.setText(f"Documento activo: {self._document_label(self.artifacts)}")
        QMessageBox.critical(self, "Error procesando archivo(s)", message)

    def _cleanup_batch_job(self) -> None:
        if self._batch_worker:
            self._batch_worker.deleteLater()
        if self._batch_thread:
            self._batch_thread.deleteLater()
        self._batch_worker = None
        self._batch_thread = None
        self._batch_progress = None

    def _on_reprocess_finished(self, result: object) -> None:
        if self._reprocess_progress:
            self._reprocess_progress.close()
        if not isinstance(result, LoadedDocument):
            return
        self._documents[self._current_document_index] = result
        self._reload_document_selector()
        self._load_document(result.artifacts, result.diagram)
        self.statusBar().showMessage(f"Reprocesamiento completado: {result.artifacts.source_pdf.name}", 5000)

    def _on_reprocess_failed(self, message: str) -> None:
        if self._reprocess_progress:
            self._reprocess_progress.close()
        QMessageBox.critical(self, "Error reprocesando", message)

    def _cleanup_reprocess_job(self) -> None:
        if self._reprocess_worker:
            self._reprocess_worker.deleteLater()
        if self._reprocess_thread:
            self._reprocess_thread.deleteLater()
        self._reprocess_worker = None
        self._reprocess_thread = None
        self._reprocess_progress = None

    def _apply_imported_web_diagram(self, imported: object) -> None:
        if not isinstance(imported, DiagramDocument):
            return
        self._documents[self._current_document_index] = LoadedDocument(
            artifacts=self.artifacts,
            diagram=imported,
        )
        self._load_document(self.artifacts, imported)
        self.store.save_diagram(self.diagram, self.artifacts.diagram_json)
        self.statusBar().showMessage("Cambios aplicados desde el modelador BPMN web.", 5000)

    def closeEvent(self, event) -> None:  # pragma: no cover - UI behavior
        self._save_draft()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # pragma: no cover - UI behavior
        super().showEvent(event)
        total_width = max(self.top_splitter.size().width(), 1000)
        total_height = max(self.main_splitter.size().height(), 700)
        self.top_splitter.setSizes(
            [
                int(total_width * 0.5),
                int(total_width * 0.5),
            ]
        )
        self.main_splitter.setSizes(
            [
                int(total_height * 0.76),
                int(total_height * 0.24),
            ]
        )
        self.source_view.reset_zoom()
        self.diagram_view.reset_zoom()

    def _select_list_item(self, widget: QListWidget, entity_id: str) -> None:
        for index in range(widget.count()):
            item = widget.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == entity_id:
                widget.blockSignals(True)
                widget.setCurrentItem(item)
                widget.blockSignals(False)
                break

    def _load_document(self, artifacts: RunArtifacts, diagram: DiagramDocument) -> None:
        self.artifacts = artifacts
        self.diagram = diagram
        normalize_event_nodes(self.diagram.nodes)
        self.diagram_title = str(diagram.metadata.get("title") or "Diagrama BPMN")
        self.processing_label.setText(f"Documento activo: {self._document_label(artifacts)}")
        self.source_panel.setTitle(f"Archivo origen: {self._document_label(artifacts)}")
        self.scene = DiagramScene(diagram)
        self.scene.node_selected.connect(self._on_node_selected)
        self.scene.edge_selected.connect(self._on_edge_selected)
        self.scene.changed_model.connect(self._on_scene_changed)
        self.diagram_view.setScene(self.scene)

        source_scene = QGraphicsScene()
        pixmap = self._load_source_pixmap(diagram.source_image)
        source_scene.addItem(QGraphicsPixmapItem(pixmap))
        source_scene.setSceneRect(pixmap.rect())
        self.source_view.setScene(source_scene)
        self.web_modeler.set_diagram(self.diagram)
        self.web_modeler.sync_from_diagram()

        self._reload_lists()
        self._refresh_issue_list()
        self._set_selection(None, None)
        self.source_view.reset_zoom()
        self.diagram_view.reset_zoom()
        self.statusBar().showMessage(f"Archivo activo: {artifacts.source_pdf}", 5000)

    def _reload_document_selector(self) -> None:
        self.document_selector.blockSignals(True)
        self.document_selector.clear()
        for item in self._documents:
            self.document_selector.addItem(self._document_label(item.artifacts))
        self.document_selector.setCurrentIndex(self._current_document_index)
        self.document_selector.blockSignals(False)

    def _on_document_selected(self, index: int) -> None:
        if index < 0 or index >= len(self._documents):
            return
        self._save_draft()
        self._current_document_index = index
        loaded = self._documents[index]
        self._load_document(loaded.artifacts, loaded.diagram)

    def _advance_to_next_document(self, reset_when_finished: bool = False) -> None:
        next_index = self._current_document_index + 1
        if next_index >= len(self._documents):
            if reset_when_finished:
                self._reset_to_initial_state()
                self.statusBar().showMessage("Se completo la cola de archivos. La aplicacion quedo lista para una nueva carga.", 5000)
                return
            self.statusBar().showMessage("No hay mas archivos en cola.", 5000)
            return
        if reset_when_finished and next_index >= len(self._documents) - 1:
            self._reset_to_initial_state()
            self.statusBar().showMessage("Se completo la cola de archivos. La aplicacion quedo lista para una nueva carga.", 5000)
            return
        self.document_selector.setCurrentIndex(next_index)
        next_name = self._documents[next_index].artifacts.source_pdf.name
        self.statusBar().showMessage(f"Siguiente archivo en cola: {next_name}", 5000)

    def _go_to_next_document(self) -> None:
        self._advance_to_next_document()

    def _show_credits(self) -> None:
        QMessageBox.information(self, "Creditos", "Desarrollado con IA por Fred Moya")

    def _reset_to_initial_state(self) -> None:
        artifacts = self.store.create_empty_run()
        blank = QPixmap(1, 1)
        blank.fill(Qt.GlobalColor.white)
        blank.save(str(artifacts.source_image), "PNG")
        diagram = DiagramDocument(
            source_pdf=artifacts.source_pdf,
            source_image=artifacts.source_image,
            image_width=1600,
            image_height=900,
            metadata={"title": "Diagrama BPMN"},
        )
        self.store.save_diagram(diagram, artifacts.diagram_json)
        self._documents = [LoadedDocument(artifacts=artifacts, diagram=diagram)]
        self._current_document_index = 0
        self._reload_document_selector()
        self._load_document(artifacts, diagram)

    def _last_input_directory(self) -> Path:
        saved = self.ui_settings.value("last_input_dir", "") or self.ui_settings.value("last_pdf_dir", "")
        if saved:
            candidate = Path(str(saved)).expanduser()
            if candidate.exists():
                return candidate
        export_parent = self.artifacts.export_vsdx.parent
        if export_parent.exists():
            return export_parent
        source_parent = self.artifacts.source_pdf.parent
        if source_parent.exists():
            return source_parent
        return Path.home()

    def _remember_input_directory(self, input_path: Path) -> None:
        self.ui_settings.setValue("last_input_dir", str(input_path.parent))

    def _default_export_path(self, export_kind: str) -> Path:
        source_name = self.artifacts.source_pdf.stem if self.artifacts.source_pdf.name != "sin_entrada.dat" else "diagrama"
        saved = self.ui_settings.value(f"last_export_dir_{export_kind}", "")
        base_dir = Path(str(saved)).expanduser() if saved else self._last_input_directory()
        if not base_dir.exists():
            base_dir = self._last_input_directory()
        suffix_map = {
            "bizagi": ".bizagi.bpmn",
            "xpdl": ".xpdl",
            "visio": ".vsdx",
        }
        suffix = suffix_map.get(export_kind, ".bpmn")
        return base_dir / f"{source_name}{suffix}"

    def _document_label(self, artifacts: RunArtifacts) -> str:
        return artifacts.source_pdf.name if artifacts.source_pdf.name != "sin_entrada.dat" else "Sin archivo"

    def _load_source_pixmap(self, image_path: Path) -> QPixmap:
        if not image_path.exists() or image_path.stat().st_size < 256:
            fallback = QPixmap(1600, 900)
            fallback.fill(Qt.GlobalColor.white)
            return fallback
        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            return pixmap
        fallback = QPixmap(1600, 900)
        fallback.fill(Qt.GlobalColor.white)
        return fallback


def _coordinate_spinbox(minimum: float = 0.0) -> QDoubleSpinBox:
    spinbox = QDoubleSpinBox()
    spinbox.setRange(minimum, 100000.0)
    spinbox.setDecimals(1)
    spinbox.setSingleStep(5.0)
    return spinbox


def launch_review_window(
    settings: Settings,
    store: LocalWorkspaceStore,
    artifacts: RunArtifacts,
    diagram: DiagramDocument,
    exporter: VisioExporter,
) -> int:
    app = QApplication.instance() or QApplication([])
    window = ReviewMainWindow(settings, store, artifacts, diagram, exporter)
    window.show()
    return app.exec()
