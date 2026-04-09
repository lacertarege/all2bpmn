from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from pdf_to_bpmn.domain import DiagramDocument, DiagramEdge, DiagramNode, EdgeType, NodeType


class XPDLExporter:
    XPDL_NS = "http://www.wfmc.org/2008/XPDL2.1"
    XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
    XSD_NS = "http://www.w3.org/2001/XMLSchema"

    def export(self, diagram: DiagramDocument, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        root = self._build_document(diagram)
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)
        return output_path

    def _build_document(self, diagram: DiagramDocument) -> ET.Element:
        ET.register_namespace("", self.XPDL_NS)
        ET.register_namespace("xsi", self.XSI_NS)

        title = str(diagram.metadata.get("title") or diagram.source_pdf.stem or "Process")
        root = ET.Element(
            _q(self.XPDL_NS, "Package"),
            {
                "Id": "Package_1",
                "Name": title,
                "OnlyOneProcess": "false",
            },
        )
        package_header = ET.SubElement(root, _q(self.XPDL_NS, "PackageHeader"))
        ET.SubElement(package_header, _q(self.XPDL_NS, "XPDLVersion")).text = "2.1"
        ET.SubElement(package_header, _q(self.XPDL_NS, "Vendor")).text = "pdf-to-bpmn-visio"
        ET.SubElement(package_header, _q(self.XPDL_NS, "Created")).text = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        ET.SubElement(root, _q(self.XPDL_NS, "RedefinableHeader"), {"PublicationStatus": "UNDER_TEST"})
        ET.SubElement(root, _q(self.XPDL_NS, "ConformanceClass"), {"GraphConformance": "NON_BLOCKED"})

        active_nodes = [node for node in diagram.nodes if not node.deleted]
        active_edges = [edge for edge in diagram.edges if not edge.deleted]
        pools = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.POOL]
        lanes = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.LANE]
        parent_map = _infer_parent_map(diagram)

        pools_el = ET.SubElement(root, _q(self.XPDL_NS, "Pools"))
        if pools:
            for index, pool in enumerate(pools, start=1):
                pool_el = ET.SubElement(
                    pools_el,
                    _q(self.XPDL_NS, "Pool"),
                {
                    "Id": pool.id,
                    "Name": pool.text or title,
                    "Process": f"Process_{index}",
                    "BoundaryVisible": "true",
                    },
                )
                self._append_node_graphics(pool_el, pool)
                self._append_pool_lanes(pool_el, pool, lanes, parent_map)
        else:
            pool_el = ET.SubElement(
                pools_el,
                _q(self.XPDL_NS, "Pool"),
                {
                    "Id": "Pool_1",
                    "Name": title,
                    "Process": "Process_1",
                    "BoundaryVisible": "true",
                },
            )
            self._append_pool_lanes(pool_el, None, lanes, parent_map)

        workflow_processes = ET.SubElement(root, _q(self.XPDL_NS, "WorkflowProcesses"))
        if pools:
            for index, pool in enumerate(pools, start=1):
                process_nodes = self._process_nodes(active_nodes, parent_map, pool.id, lanes)
                self._append_process(
                    workflow_processes,
                    diagram,
                    f"Process_{index}",
                    pool.text or title,
                    active_nodes,
                    active_edges,
                    parent_map,
                    pool.id,
                    lanes,
                )
                self._update_pool_geometry(pool_el, pool, process_nodes, lanes, parent_map)
        else:
            process_nodes = self._process_nodes(active_nodes, parent_map, None, lanes)
            self._append_process(
                workflow_processes,
                diagram,
                "Process_1",
                title,
                active_nodes,
                active_edges,
                parent_map,
                None,
                lanes,
            )
            self._update_pool_geometry(pool_el, None, process_nodes, lanes, parent_map)
        return root

    def _append_pool_lanes(
        self,
        pool_el: ET.Element,
        pool: DiagramNode | None,
        lanes: list[DiagramNode],
        parent_map: dict[str, str | None],
    ) -> None:
        relevant_lanes = [
            lane for lane in lanes if parent_map.get(lane.id) == (pool.id if pool is not None else None)
        ]
        if not relevant_lanes:
            return
        lanes_el = ET.SubElement(pool_el, _q(self.XPDL_NS, "Lanes"))
        for lane in relevant_lanes:
            lane_el = ET.SubElement(
                lanes_el,
                _q(self.XPDL_NS, "Lane"),
                {
                    "Id": lane.id,
                    "Name": lane.text or lane.id,
                    "ParentPool": pool.id if pool is not None else "Pool_1",
                },
            )
            self._append_node_graphics(lane_el, lane)

    def _update_pool_geometry(
        self,
        pool_el: ET.Element,
        pool: DiagramNode | None,
        process_nodes: list[DiagramNode],
        lanes: list[DiagramNode],
        parent_map: dict[str, str | None],
    ) -> None:
        if not process_nodes:
            return
        lane_nodes = [lane for lane in lanes if parent_map.get(lane.id) == (pool.id if pool is not None else None)]
        pool_box = _container_bounds(process_nodes, pool)
        self._set_node_graphics(pool_el, pool_box)

        lanes_el = pool_el.find(_q(self.XPDL_NS, "Lanes"))
        if lanes_el is None:
            return
        lane_map = {lane.id: lane for lane in lane_nodes}
        for lane_el in lanes_el.findall(_q(self.XPDL_NS, "Lane")):
            lane_id = lane_el.get("Id", "")
            lane = lane_map.get(lane_id)
            if lane is None:
                continue
            contained = [
                node
                for node in process_nodes
                if parent_map.get(node.id) == lane_id
            ]
            lane_box = _container_bounds(contained or process_nodes, lane)
            self._set_node_graphics(lane_el, lane_box)

    def _append_process(
        self,
        workflow_processes: ET.Element,
        diagram: DiagramDocument,
        process_id: str,
        process_name: str,
        active_nodes: list[DiagramNode],
        active_edges: list[DiagramEdge],
        parent_map: dict[str, str | None],
        pool_id: str | None,
        lanes: list[DiagramNode],
    ) -> None:
        process = ET.SubElement(
            workflow_processes,
            _q(self.XPDL_NS, "WorkflowProcess"),
            {"Id": process_id, "Name": process_name},
        )
        ET.SubElement(process, _q(self.XPDL_NS, "ProcessHeader"), {"DurationUnit": "D"})
        activities = ET.SubElement(process, _q(self.XPDL_NS, "Activities"))
        pool_lanes = [lane for lane in lanes if parent_map.get(lane.id) == pool_id]
        exported_activity_ids: set[str] = set()

        for node in active_nodes:
            node_type = _coerce_node_type(node.node_type)
            if node_type in {NodeType.POOL, NodeType.LANE}:
                continue
            if node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
                continue
            if pool_id is not None and not _belongs_to_process(node.id, pool_id, parent_map, pool_lanes):
                continue
            activity = ET.SubElement(
                activities,
                _q(self.XPDL_NS, "Activity"),
                {"Id": node.id, "Name": node.text or node.id},
            )
            self._append_activity_kind(activity, node_type)
            ET.SubElement(activity, _q(self.XPDL_NS, "Documentation"))
            ET.SubElement(activity, _q(self.XPDL_NS, "ExtendedAttributes"))
            self._append_node_graphics(activity, node)
            ET.SubElement(activity, _q(self.XPDL_NS, "IsForCompensationSpecified")).text = "false"
            exported_activity_ids.add(node.id)

        transitions = ET.SubElement(process, _q(self.XPDL_NS, "Transitions"))
        for edge in active_edges:
            source = diagram.find_node(edge.source_id)
            target = diagram.find_node(edge.target_id)
            if not source or not target or source.deleted or target.deleted:
                continue
            if edge.source_id not in exported_activity_ids or edge.target_id not in exported_activity_ids:
                continue
            if _coerce_edge_type(edge.edge_type) != EdgeType.SEQUENCE_FLOW:
                continue
            if pool_id is not None:
                if not _belongs_to_process(source.id, pool_id, parent_map, pool_lanes):
                    continue
                if not _belongs_to_process(target.id, pool_id, parent_map, pool_lanes):
                    continue
            transition = ET.SubElement(
                transitions,
                _q(self.XPDL_NS, "Transition"),
                {
                    "Id": edge.id,
                    "From": edge.source_id,
                    "To": edge.target_id,
                },
            )
            if _has_meaningful_edge_label(edge):
                transition.set("Name", edge.text.strip())
            self._append_connector_graphics(transition, edge, source, target)
        ET.SubElement(process, _q(self.XPDL_NS, "ExtendedAttributes"))

    def _append_activity_kind(self, activity: ET.Element, node_type: NodeType) -> None:
        if node_type == NodeType.START_EVENT:
            event = ET.SubElement(activity, _q(self.XPDL_NS, "Event"))
            ET.SubElement(event, _q(self.XPDL_NS, "StartEvent"), {"Trigger": "None"})
            return
        if node_type == NodeType.END_EVENT:
            event = ET.SubElement(activity, _q(self.XPDL_NS, "Event"))
            ET.SubElement(event, _q(self.XPDL_NS, "EndEvent"))
            return
        if node_type in {NodeType.INTERMEDIATE_EVENT, NodeType.BOUNDARY_EVENT}:
            event = ET.SubElement(activity, _q(self.XPDL_NS, "Event"))
            ET.SubElement(event, _q(self.XPDL_NS, "IntermediateEvent"), {"Trigger": "None"})
            return
        if node_type in {
            NodeType.EXCLUSIVE_GATEWAY,
            NodeType.PARALLEL_GATEWAY,
            NodeType.INCLUSIVE_GATEWAY,
            NodeType.EVENT_BASED_GATEWAY,
        }:
            ET.SubElement(activity, _q(self.XPDL_NS, "Route"))
            return
        implementation = ET.SubElement(activity, _q(self.XPDL_NS, "Implementation"))
        ET.SubElement(implementation, _q(self.XPDL_NS, "Task"))

    def _append_node_graphics(self, parent: ET.Element, node: DiagramNode) -> None:
        box = _shape_bounds(node)
        self._set_node_graphics(parent, box)

    def _set_node_graphics(self, parent: ET.Element, box: dict[str, float]) -> None:
        for existing in parent.findall(_q(self.XPDL_NS, "NodeGraphicsInfos")):
            parent.remove(existing)
        infos = ET.SubElement(parent, _q(self.XPDL_NS, "NodeGraphicsInfos"))
        info = ET.SubElement(
            infos,
            _q(self.XPDL_NS, "NodeGraphicsInfo"),
            {
                "Width": _fmt(box["width"]),
                "Height": _fmt(box["height"]),
            },
        )
        ET.SubElement(
            info,
            _q(self.XPDL_NS, "Coordinates"),
            {"XCoordinate": _fmt(box["x"]), "YCoordinate": _fmt(box["y"])},
        )

    def _process_nodes(
        self,
        active_nodes: list[DiagramNode],
        parent_map: dict[str, str | None],
        pool_id: str | None,
        lanes: list[DiagramNode],
    ) -> list[DiagramNode]:
        pool_lanes = [lane for lane in lanes if parent_map.get(lane.id) == pool_id]
        included: list[DiagramNode] = []
        for node in active_nodes:
            node_type = _coerce_node_type(node.node_type)
            if node_type in {NodeType.POOL, NodeType.LANE, NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
                continue
            if pool_id is not None and not _belongs_to_process(node.id, pool_id, parent_map, pool_lanes):
                continue
            included.append(node)
        return included

    def _append_connector_graphics(
        self,
        parent: ET.Element,
        edge: DiagramEdge,
        source: DiagramNode,
        target: DiagramNode,
    ) -> None:
        infos = ET.SubElement(parent, _q(self.XPDL_NS, "ConnectorGraphicsInfos"))
        info = ET.SubElement(
            infos,
            _q(self.XPDL_NS, "ConnectorGraphicsInfo"),
        )
        points = edge.waypoints or [source.center, target.center]
        if len(points) < 2:
            points = [source.center, target.center]
        for point in points:
            ET.SubElement(
                info,
                _q(self.XPDL_NS, "Coordinates"),
                {"XCoordinate": _fmt(point.x), "YCoordinate": _fmt(point.y)},
            )

    def _append_extended_attributes(self, parent: ET.Element, attributes: dict[str, str]) -> None:
        filtered = {key: value for key, value in attributes.items() if str(value).strip()}
        if not filtered:
            ET.SubElement(parent, _q(self.XPDL_NS, "ExtendedAttributes"))
            return
        attrs = ET.SubElement(parent, _q(self.XPDL_NS, "ExtendedAttributes"))
        for name, value in filtered.items():
            ET.SubElement(attrs, _q(self.XPDL_NS, "ExtendedAttribute"), {"Name": name, "Value": value})


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _has_meaningful_edge_label(edge: DiagramEdge) -> bool:
    text = edge.text.strip()
    if not text:
        return False
    normalized = text.lower()
    if normalized == edge.id.lower():
        return False
    if normalized.startswith("flow_") or normalized.startswith("edge-"):
        return False
    return True


def _shape_bounds(node: DiagramNode) -> dict[str, float]:
    node_type = _coerce_node_type(node.node_type)
    if node_type in {
        NodeType.START_EVENT,
        NodeType.END_EVENT,
        NodeType.INTERMEDIATE_EVENT,
        NodeType.BOUNDARY_EVENT,
    }:
        size = float(max(24.0, min(node.width, node.height)))
        return {
            "x": node.center.x - (size / 2.0),
            "y": node.center.y - (size / 2.0),
            "width": size,
            "height": size,
        }
    if node_type in {
        NodeType.EXCLUSIVE_GATEWAY,
        NodeType.PARALLEL_GATEWAY,
        NodeType.INCLUSIVE_GATEWAY,
        NodeType.EVENT_BASED_GATEWAY,
    }:
        size = float(max(34.0, min(node.width, node.height)))
        return {
            "x": node.center.x - (size / 2.0),
            "y": node.center.y - (size / 2.0),
            "width": size,
            "height": size,
        }
    return {
        "x": float(node.x),
        "y": float(node.y),
        "width": float(node.width),
        "height": float(node.height),
    }


def _container_bounds(
    contained_nodes: list[DiagramNode],
    container: DiagramNode | None,
    fallback: dict[str, float] | None = None,
) -> dict[str, float]:
    if not contained_nodes:
        if container is not None:
            return _shape_bounds(container)
        return fallback or {"x": 0.0, "y": 0.0, "width": 400.0, "height": 180.0}
    left_gutter = 72.0
    outer_margin = 24.0
    top_margin = 18.0
    bottom_margin = 18.0
    min_x = min(_shape_bounds(node)["x"] for node in contained_nodes)
    min_y = min(_shape_bounds(node)["y"] for node in contained_nodes)
    max_right = max(_shape_bounds(node)["x"] + _shape_bounds(node)["width"] for node in contained_nodes)
    max_bottom = max(_shape_bounds(node)["y"] + _shape_bounds(node)["height"] for node in contained_nodes)
    x = min_x - left_gutter - outer_margin
    y = min_y - top_margin
    width = (max_right - min_x) + left_gutter + (outer_margin * 2.0)
    height = (max_bottom - min_y) + top_margin + bottom_margin
    if container is not None:
        original = _shape_bounds(container)
        x = min(x, original["x"])
        y = min(y, original["y"])
        width = max(width, (original["x"] + original["width"]) - x)
        height = max(height, (original["y"] + original["height"]) - y)
    if fallback is not None:
        x = min(x, fallback["x"])
        y = min(y, fallback["y"])
        width = max(width, (fallback["x"] + fallback["width"]) - x)
        height = max(height, (fallback["y"] + fallback["height"]) - y)
    return {
        "x": float(max(0.0, x)),
        "y": float(max(0.0, y)),
        "width": float(max(80.0, width)),
        "height": float(max(60.0, height)),
    }


def _coerce_node_type(value) -> NodeType:
    if isinstance(value, NodeType):
        return value
    text = str(value or "").strip().lower()
    for node_type in NodeType:
        if node_type.value == text:
            return node_type
    return NodeType.TASK


def _coerce_edge_type(value) -> EdgeType:
    if isinstance(value, EdgeType):
        return value
    text = str(value or "").strip().lower()
    for edge_type in EdgeType:
        if edge_type.value == text:
            return edge_type
    return EdgeType.SEQUENCE_FLOW


def _infer_parent_map(diagram: DiagramDocument) -> dict[str, str | None]:
    active_nodes = [node for node in diagram.nodes if not node.deleted]
    pools = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.POOL]
    lanes = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.LANE]
    parent_map: dict[str, str | None] = {}

    for node in active_nodes:
        node_type = _coerce_node_type(node.node_type)
        if node.parent_id and diagram.find_node(node.parent_id):
            parent_map[node.id] = node.parent_id
            continue
        if node_type == NodeType.POOL:
            parent_map[node.id] = None
            continue
        if node_type == NodeType.LANE:
            parent = _smallest_container(node, pools)
            parent_map[node.id] = parent.id if parent else None
            continue
        lane_parent = _smallest_container(node, lanes)
        if lane_parent:
            parent_map[node.id] = lane_parent.id
            continue
        pool_parent = _smallest_container(node, pools)
        parent_map[node.id] = pool_parent.id if pool_parent else None
    return parent_map


def _smallest_container(node: DiagramNode, containers: list[DiagramNode]) -> DiagramNode | None:
    matches = [candidate for candidate in containers if candidate.id != node.id and _contains(candidate, node)]
    if not matches:
        return None
    matches.sort(key=lambda item: item.width * item.height)
    return matches[0]


def _contains(container: DiagramNode, node: DiagramNode) -> bool:
    margin = 2.0
    return (
        node.x >= container.x - margin
        and node.y >= container.y - margin
        and (node.x + node.width) <= (container.x + container.width + margin)
        and (node.y + node.height) <= (container.y + container.height + margin)
    )


def _belongs_to_process(
    node_id: str, pool_id: str, parent_map: dict[str, str | None], pool_lanes: list[DiagramNode]
) -> bool:
    parent_id = parent_map.get(node_id)
    if parent_id == pool_id:
        return True
    if parent_id is None:
        return False
    return any(parent_id == lane.id for lane in pool_lanes)
