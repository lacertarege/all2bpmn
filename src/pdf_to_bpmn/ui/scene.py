from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QBrush,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QGraphicsObject, QGraphicsScene, QGraphicsView

from pdf_to_bpmn.domain import DiagramDocument, DiagramEdge, DiagramNode, EdgeType, NodeType


SUBPROCESS_FONT_SIZE = 11


class ZoomableGraphicsView(QGraphicsView):
    def __init__(self, auto_fit: bool = False) -> None:
        super().__init__()
        self._auto_fit = auto_fit
        self._user_zoomed = False
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def wheelEvent(self, event) -> None:  # pragma: no cover - UI behavior
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._user_zoomed = True
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def setScene(self, scene: QGraphicsScene) -> None:
        super().setScene(scene)
        if self._auto_fit:
            self.fit_to_scene()

    def resizeEvent(self, event) -> None:  # pragma: no cover - UI behavior
        super().resizeEvent(event)
        if self._auto_fit and not self._user_zoomed:
            self.fit_to_scene()

    def showEvent(self, event) -> None:  # pragma: no cover - UI behavior
        super().showEvent(event)
        if self._auto_fit and not self._user_zoomed:
            self.fit_to_scene()

    def reset_zoom(self) -> None:
        self._user_zoomed = False
        self.fit_to_scene()

    def fit_to_scene(self) -> None:
        scene = self.scene()
        if not scene:
            return
        rect = scene.itemsBoundingRect()
        if rect.isNull() or rect.isEmpty():
            rect = scene.sceneRect()
        if rect.isNull() or rect.isEmpty():
            return
        margin = 24.0
        self.fitInView(rect.adjusted(-margin, -margin, margin, margin), Qt.AspectRatioMode.KeepAspectRatio)


class NodeItem(QGraphicsObject):
    moved = Signal(str)
    selected_item = Signal(str)

    def __init__(self, node: DiagramNode) -> None:
        super().__init__()
        self.node = node
        self.setFlags(
            QGraphicsObject.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsObject.GraphicsItemFlag.ItemIsMovable
            | QGraphicsObject.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setPos(node.x, node.y)
        if node.node_type == NodeType.POOL:
            self.setZValue(2)
        elif node.node_type == NodeType.LANE:
            self.setZValue(1)
        else:
            self.setZValue(10)

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self.node.width, self.node.height)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        if self.node.node_type == NodeType.DATA_STORE:
            path.addPath(self._data_store_path())
            return path
        if self.node.node_type in {
            NodeType.START_EVENT,
            NodeType.INTERMEDIATE_EVENT,
            NodeType.END_EVENT,
            NodeType.BOUNDARY_EVENT,
        }:
            path.addEllipse(self.boundingRect())
            return path
        if self.node.node_type in {
            NodeType.EXCLUSIVE_GATEWAY,
            NodeType.PARALLEL_GATEWAY,
            NodeType.INCLUSIVE_GATEWAY,
            NodeType.EVENT_BASED_GATEWAY,
        }:
            path.addPolygon(self._diamond_polygon())
            return path
        if self.node.node_type == NodeType.ANNOTATION:
            path.addRect(self.boundingRect())
            return path
        path.addRoundedRect(self.boundingRect(), 8.0, 8.0)
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:  # pragma: no cover - UI behavior
        painter.save()
        if self.node.deleted:
            painter.setOpacity(0.25)

        fill = QColor("#f7fafc")
        border = QColor("#0f172a")
        if self.node.node_type == NodeType.POOL:
            fill = QColor(0, 0, 0, 0)
        elif self.node.node_type == NodeType.LANE:
            fill = QColor("#edf2f7")
        elif self.node.node_type == NodeType.START_EVENT:
            fill = QColor("#dcfce7")
            border = QColor("#16a34a")
        elif self.node.node_type == NodeType.END_EVENT:
            fill = QColor("#fee2e2")
            border = QColor("#dc2626")
        elif self.node.node_type in {
            NodeType.INTERMEDIATE_EVENT,
            NodeType.BOUNDARY_EVENT,
        }:
            fill = QColor("#eff6ff")
            border = QColor("#2563eb")
        elif self.node.node_type in {
            NodeType.EXCLUSIVE_GATEWAY,
            NodeType.PARALLEL_GATEWAY,
            NodeType.INCLUSIVE_GATEWAY,
            NodeType.EVENT_BASED_GATEWAY,
        }:
            fill = QColor("#fff7ed")
            border = QColor("#c2410c")
        elif self.node.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
            fill = QColor("#faf5ff")
            border = QColor("#6b21a8")

        pen = QPen(border, 2.0 if not self.isSelected() else 3.2)
        if self.node.node_type == NodeType.ANNOTATION:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))

        if self.node.node_type in {
            NodeType.START_EVENT,
            NodeType.INTERMEDIATE_EVENT,
            NodeType.END_EVENT,
            NodeType.BOUNDARY_EVENT,
        }:
            painter.drawEllipse(self.boundingRect())
            if self.node.node_type in {NodeType.INTERMEDIATE_EVENT, NodeType.BOUNDARY_EVENT}:
                inner = self.boundingRect().adjusted(5, 5, -5, -5)
                painter.drawEllipse(inner)
        elif self.node.node_type in {
            NodeType.EXCLUSIVE_GATEWAY,
            NodeType.PARALLEL_GATEWAY,
            NodeType.INCLUSIVE_GATEWAY,
            NodeType.EVENT_BASED_GATEWAY,
        }:
            painter.drawPolygon(self._diamond_polygon())
        elif self.node.node_type == NodeType.POOL:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.boundingRect())
        elif self.node.node_type == NodeType.DATA_STORE:
            painter.drawPath(self._data_store_path())
            self._draw_data_store_top(painter)
        else:
            painter.drawRoundedRect(self.boundingRect(), 8.0, 8.0)
            if self.node.node_type == NodeType.COLLAPSED_SUBPROCESS:
                self._draw_collapsed_marker(painter)

        if self.node.node_type == NodeType.POOL:
            self._draw_pool_label_band(painter)

        self._draw_node_label(painter)
        painter.restore()

    def sync_from_model(self) -> None:
        self.prepareGeometryChange()
        self.setPos(self.node.x, self.node.y)
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsObject.GraphicsItemChange.ItemPositionHasChanged:
            self.node.x = float(value.x())
            self.node.y = float(value.y())
            self.moved.emit(self.node.id)
        elif change == QGraphicsObject.GraphicsItemChange.ItemSelectedHasChanged and bool(value):
            self.selected_item.emit(self.node.id)
        return super().itemChange(change, value)

    def _diamond_polygon(self) -> QPolygonF:
        rect = self.boundingRect()
        return QPolygonF(
            [
                QPointF(rect.center().x(), rect.top()),
                QPointF(rect.right(), rect.center().y()),
                QPointF(rect.center().x(), rect.bottom()),
                QPointF(rect.left(), rect.center().y()),
            ]
        )

    def _data_store_path(self) -> QPainterPath:
        rect = self.boundingRect()
        ellipse_h = min(max(rect.height() * 0.22, 10.0), 18.0)
        path = QPainterPath()
        path.moveTo(rect.left(), rect.top() + ellipse_h / 2.0)
        path.quadTo(rect.center().x(), rect.top() - ellipse_h / 2.0, rect.right(), rect.top() + ellipse_h / 2.0)
        path.lineTo(rect.right(), rect.bottom() - ellipse_h / 2.0)
        path.quadTo(rect.center().x(), rect.bottom() + ellipse_h / 2.0, rect.left(), rect.bottom() - ellipse_h / 2.0)
        path.closeSubpath()
        return path

    def _draw_data_store_top(self, painter: QPainter) -> None:
        rect = self.boundingRect()
        ellipse_h = min(max(rect.height() * 0.22, 10.0), 18.0)
        painter.drawEllipse(QRectF(rect.left(), rect.top(), rect.width(), ellipse_h))

    def _draw_collapsed_marker(self, painter: QPainter) -> None:
        rect = self.boundingRect()
        marker_width = min(16.0, max(rect.width() * 0.18, 12.0))
        marker_height = min(12.0, max(rect.height() * 0.16, 10.0))
        marker_rect = QRectF(
            rect.center().x() - marker_width / 2.0,
            rect.bottom() - marker_height - 6.0,
            marker_width,
            marker_height,
        )
        painter.save()
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor("#0f172a"), 1.4))
        painter.drawRect(marker_rect)
        painter.drawLine(
            QPointF(marker_rect.left() + 3.0, marker_rect.center().y()),
            QPointF(marker_rect.right() - 3.0, marker_rect.center().y()),
        )
        painter.restore()

    def _draw_node_label(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor("#111827")))
        text = self._display_text()

        if self.node.node_type == NodeType.LANE:
            self._draw_vertical_lane_label(painter, text)
            return
        if self.node.node_type == NodeType.POOL:
            self._draw_vertical_pool_label(painter, text)
            return

        text_rect = self.boundingRect().adjusted(4, 4, -4, -4)
        if self.node.node_type == NodeType.COLLAPSED_SUBPROCESS:
            text_rect = text_rect.adjusted(0, 0, 0, -20)
        font = self._fitted_font_for_rect(text, text_rect, self.node.node_type)
        painter.setFont(font)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            text,
        )

    def _display_text(self) -> str:
        return self.node.text or self.node.node_type.value

    def _draw_vertical_lane_label(self, painter: QPainter, text: str) -> None:
        rect = self.boundingRect()
        label_band = min(42.0, max(28.0, rect.width() * 0.14))
        vertical_rect = QRectF(0.0, 0.0, rect.height(), label_band)
        painter.save()
        painter.translate(label_band, rect.height())
        painter.rotate(-90.0)
        lane_font = self._fitted_font_for_rect(
            text,
            vertical_rect.adjusted(4.0, 4.0, -4.0, -4.0),
            NodeType.LANE,
            min_size=9.0,
            max_size=13.0,
        )
        painter.setFont(lane_font)
        painter.drawText(
            vertical_rect.adjusted(4.0, 4.0, -4.0, -4.0),
            int(Qt.AlignmentFlag.AlignCenter),
            text,
        )
        painter.restore()

    def _draw_pool_label_band(self, painter: QPainter) -> None:
        rect = self.boundingRect()
        label_band = self._pool_label_band_width(rect)
        band_rect = QRectF(0.0, 0.0, label_band, rect.height())
        painter.save()
        painter.setPen(QPen(QColor("#0f172a"), 1.6))
        painter.setBrush(QBrush(QColor("#e2e8f0")))
        painter.drawRect(band_rect)
        painter.restore()

    def _draw_vertical_pool_label(self, painter: QPainter, text: str) -> None:
        rect = self.boundingRect()
        label_band = self._pool_label_band_width(rect)
        vertical_rect = QRectF(0.0, 0.0, rect.height(), label_band)
        painter.save()
        painter.translate(label_band, rect.height())
        painter.rotate(-90.0)
        pool_font = self._fitted_font_for_rect(
            text,
            vertical_rect.adjusted(6.0, 6.0, -6.0, -6.0),
            NodeType.POOL,
            min_size=10.0,
            max_size=14.0,
            bold=True,
        )
        painter.setFont(pool_font)
        painter.drawText(
            vertical_rect.adjusted(6.0, 6.0, -6.0, -6.0),
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            text,
        )
        painter.restore()

    def _pool_label_band_width(self, rect: QRectF) -> float:
        return min(54.0, max(34.0, rect.width() * 0.16))

    def _fitted_font_for_rect(
        self,
        text: str,
        rect: QRectF,
        node_type: NodeType,
        min_size: float | None = None,
        max_size: float | None = None,
        bold: bool = False,
    ) -> QFont:
        font = QFont("Segoe UI")
        font.setBold(bold)
        if not text.strip():
            font.setPointSizeF(11.0)
            return font

        base_min = 8.0 if min_size is None else min_size
        if node_type in {
            NodeType.TASK,
            NodeType.USER_TASK,
            NodeType.SERVICE_TASK,
            NodeType.SUBPROCESS,
            NodeType.COLLAPSED_SUBPROCESS,
        }:
            preferred = max(
                SUBPROCESS_FONT_SIZE,
                min(rect.height() * 0.16, rect.width() * 0.11),
            )
            base_max = 18.0 if max_size is None else max_size
        elif node_type in {
            NodeType.START_EVENT,
            NodeType.INTERMEDIATE_EVENT,
            NodeType.END_EVENT,
            NodeType.BOUNDARY_EVENT,
        }:
            preferred = min(rect.height() * 0.28, rect.width() * 0.22)
            base_max = 14.0 if max_size is None else max_size
        else:
            preferred = min(rect.height() * 0.18, rect.width() * 0.12)
            base_max = 16.0 if max_size is None else max_size

        size = max(base_min, min(base_max, preferred))
        flags = int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap)
        while size > base_min:
            font.setPointSizeF(size)
            metrics = QFontMetricsF(font)
            bounds = metrics.boundingRect(rect, flags, text)
            if bounds.width() <= rect.width() + 1.0 and bounds.height() <= rect.height() + 1.0:
                break
            size -= 0.5
        font.setPointSizeF(max(base_min, size))
        return font


class EdgeItem(QGraphicsObject):
    selected_item = Signal(str)

    def __init__(
        self, edge: DiagramEdge, source_item: NodeItem | None, target_item: NodeItem | None
    ) -> None:
        super().__init__()
        self.edge = edge
        self.source_item = source_item
        self.target_item = target_item
        self._path = QPainterPath()
        self.setFlags(QGraphicsObject.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(5)
        self.refresh_path()

    def boundingRect(self) -> QRectF:
        return self._path.boundingRect().adjusted(-10, -10, 10, 10)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(12.0)
        return stroker.createStroke(self._path)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # pragma: no cover - UI behavior
        if not self.source_item or not self.target_item:
            return
        painter.save()
        if self.edge.deleted:
            painter.setOpacity(0.25)
        color = QColor("#2563eb")
        if self.edge.edge_type == EdgeType.MESSAGE_FLOW:
            color = QColor("#7c3aed")
        elif self.edge.edge_type == EdgeType.ASSOCIATION:
            color = QColor("#64748b")

        pen = QPen(color, 2.4 if not self.isSelected() else 3.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if self.edge.edge_type in {EdgeType.MESSAGE_FLOW, EdgeType.ASSOCIATION}:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._path)
        self._draw_arrow_head(painter, color)
        if self.edge.text:
            mid = self._path.pointAtPercent(0.5)
            painter.setPen(QPen(QColor("#111827")))
            painter.setFont(QFont("Segoe UI", 12))
            painter.drawText(QRectF(mid.x() - 90, mid.y() - 20, 180, 28), self.edge.text)
        painter.restore()

    def refresh_path(self) -> None:
        self.prepareGeometryChange()
        self._path = QPainterPath()
        if not self.source_item or not self.target_item:
            self.update()
            return

        if self.edge.waypoints:
            inner_points = [
                QPointF(point.x, point.y)
                for point in self.edge.waypoints[1:-1]
            ]
            source_center = self._center_for(self.source_item)
            target_center = self._center_for(self.target_item)
            first_reference = inner_points[0] if inner_points else target_center
            last_reference = inner_points[-1] if inner_points else source_center
            source_point = self._connection_point(self.source_item, first_reference)
            target_point = self._connection_point(self.target_item, last_reference)
            points = [source_point, *inner_points, target_point]
            self._path.moveTo(points[0])
            for point in points[1:]:
                self._path.lineTo(point)
        else:
            source_center = self._center_for(self.source_item)
            target_center = self._center_for(self.target_item)
            source_point = self._connection_point(self.source_item, target_center)
            target_point = self._connection_point(self.target_item, source_center)
            self._path.moveTo(source_point)
            dx = target_point.x() - source_point.x()
            dy = target_point.y() - source_point.y()
            if abs(dx) > abs(dy):
                mid = QPointF(source_point.x() + dx / 2.0, source_point.y())
                self._path.lineTo(mid)
                self._path.lineTo(QPointF(mid.x(), target_point.y()))
            else:
                mid = QPointF(source_point.x(), source_point.y() + dy / 2.0)
                self._path.lineTo(mid)
                self._path.lineTo(QPointF(target_point.x(), mid.y()))
            self._path.lineTo(target_point)
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsObject.GraphicsItemChange.ItemSelectedHasChanged and bool(value):
            self.selected_item.emit(self.edge.id)
        return super().itemChange(change, value)

    def _center_for(self, item: NodeItem) -> QPointF:
        rect = item.boundingRect()
        return item.mapToScene(rect.center())

    def _connection_point(self, item: NodeItem, toward: QPointF) -> QPointF:
        rect = item.boundingRect()
        center = rect.center()
        local_toward = item.mapFromScene(toward)
        dx = local_toward.x() - center.x()
        dy = local_toward.y() - center.y()
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return item.mapToScene(center)

        half_w = max(rect.width() / 2.0, 1.0)
        half_h = max(rect.height() / 2.0, 1.0)

        if item.node.node_type in {
            NodeType.START_EVENT,
            NodeType.INTERMEDIATE_EVENT,
            NodeType.END_EVENT,
            NodeType.BOUNDARY_EVENT,
        }:
            length = math.hypot(dx, dy)
            scale = min(half_w, half_h) / max(length, 1.0)
            point = QPointF(center.x() + dx * scale, center.y() + dy * scale)
            return item.mapToScene(point)

        scale = 1.0 / max(abs(dx) / half_w, abs(dy) / half_h, 1.0)
        point = QPointF(center.x() + dx * scale, center.y() + dy * scale)
        return item.mapToScene(point)

    def _draw_arrow_head(self, painter: QPainter, color: QColor) -> None:
        if self._path.elementCount() < 2:
            return
        end = self._path.currentPosition()
        prev = self._path.pointAtPercent(0.97)
        angle = math.atan2(end.y() - prev.y(), end.x() - prev.x())
        size = 12.0 if self.edge.edge_type == EdgeType.SEQUENCE_FLOW else 10.0
        left = QPointF(
            end.x() - size * math.cos(angle - math.pi / 6),
            end.y() - size * math.sin(angle - math.pi / 6),
        )
        right = QPointF(
            end.x() - size * math.cos(angle + math.pi / 6),
            end.y() - size * math.sin(angle + math.pi / 6),
        )
        painter.setBrush(QBrush(color))
        painter.drawPolygon(QPolygonF([end, left, right]))


class DiagramScene(QGraphicsScene):
    node_selected = Signal(str)
    edge_selected = Signal(str)
    changed_model = Signal()

    def __init__(self, diagram: DiagramDocument) -> None:
        super().__init__()
        self.diagram = diagram
        self.node_items: dict[str, NodeItem] = {}
        self.edge_items: dict[str, EdgeItem] = {}
        self.setSceneRect(0, 0, diagram.image_width, diagram.image_height)
        self.reload()

    def reload(self) -> None:
        self.clear()
        self.node_items.clear()
        self.edge_items.clear()

        for node in sorted(
            self.diagram.nodes,
            key=lambda item: 0 if item.node_type in {NodeType.POOL, NodeType.LANE} else 1,
        ):
            item = NodeItem(node)
            item.moved.connect(self._on_node_moved)
            item.selected_item.connect(self.node_selected.emit)
            self.addItem(item)
            self.node_items[node.id] = item

        for edge in self.diagram.edges:
            edge_item = EdgeItem(
                edge,
                self.node_items.get(edge.source_id),
                self.node_items.get(edge.target_id),
            )
            edge_item.selected_item.connect(self.edge_selected.emit)
            self.addItem(edge_item)
            self.edge_items[edge.id] = edge_item

    def refresh_node(self, node_id: str) -> None:
        item = self.node_items[node_id]
        item.sync_from_model()
        self._refresh_edges_for(node_id)
        self.changed_model.emit()

    def refresh_edge(self, edge_id: str) -> None:
        item = self.edge_items[edge_id]
        item.edge = self.diagram.find_edge(edge_id) or item.edge
        item.source_item = self.node_items.get(item.edge.source_id)
        item.target_item = self.node_items.get(item.edge.target_id)
        item.refresh_path()
        self.changed_model.emit()

    def select_node(self, node_id: str) -> None:
        item = self.node_items.get(node_id)
        if not item:
            return
        self.clearSelection()
        item.setSelected(True)

    def select_edge(self, edge_id: str) -> None:
        item = self.edge_items.get(edge_id)
        if not item:
            return
        self.clearSelection()
        item.setSelected(True)

    def select_entities(self, node_ids: list[str] | set[str], edge_ids: list[str] | set[str]) -> None:
        self.clearSelection()
        for node_id in node_ids:
            item = self.node_items.get(node_id)
            if item:
                item.setSelected(True)
        for edge_id in edge_ids:
            item = self.edge_items.get(edge_id)
            if item:
                item.setSelected(True)

    def _on_node_moved(self, node_id: str) -> None:
        self._refresh_edges_for(node_id)
        self.changed_model.emit()

    def _refresh_edges_for(self, node_id: str) -> None:
        for edge in self.diagram.edges:
            if edge.source_id == node_id or edge.target_id == node_id:
                edge_item = self.edge_items.get(edge.id)
                if edge_item:
                    edge_item.refresh_path()
