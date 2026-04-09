from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import DiagramDocument, DiagramEdge, DiagramNode, EdgeType, NodeType


class BPMNSemanticExporter:
    BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
    BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
    DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
    DI_NS = "http://www.omg.org/spec/DD/20100524/DI"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def export(self, diagram: DiagramDocument, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        parent_map = _infer_parent_map(diagram)
        root = self._build_document(diagram, parent_map)
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)
        self._normalize_with_open_bpmn(output_path)
        return output_path

    def _build_document(self, diagram: DiagramDocument, parent_map: dict[str, str | None]) -> ET.Element:
        ET.register_namespace("bpmn", self.BPMN_NS)
        ET.register_namespace("bpmndi", self.BPMNDI_NS)
        ET.register_namespace("dc", self.DC_NS)
        ET.register_namespace("di", self.DI_NS)

        definitions = ET.Element(
            _q(self.BPMN_NS, "definitions"),
            {
                "id": "Definitions_1",
                "targetNamespace": "https://pdf-to-bpmn.local",
                "exporter": "pdf-to-bpmn-visio",
                "exporterVersion": "0.1.0",
            },
        )
        title = str(diagram.metadata.get("title") or diagram.source_pdf.stem or "Process")
        active_nodes = [node for node in diagram.nodes if not node.deleted]
        active_edges = [edge for edge in diagram.edges if not edge.deleted]
        pools = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.POOL]
        lanes = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.LANE]

        has_collaboration = bool(pools or any(_coerce_edge_type(edge.edge_type) == EdgeType.MESSAGE_FLOW for edge in active_edges))
        collaboration = (
            ET.SubElement(definitions, _q(self.BPMN_NS, "collaboration"), {"id": "Collaboration_1"})
            if has_collaboration
            else None
        )
        lane_process_map: dict[str, str] = {}
        pool_process_map: dict[str, str] = {}
        plane_element = "Collaboration_1" if has_collaboration else "Process_1"

        if pools:
            for index, pool in enumerate(pools, start=1):
                process_id = f"Process_{index}"
                pool_process_map[pool.id] = process_id
                if collaboration is not None:
                    ET.SubElement(
                        collaboration,
                        _q(self.BPMN_NS, "participant"),
                        {
                            "id": pool.id,
                            "name": pool.text or title,
                            "processRef": process_id,
                        },
                    )
                process = ET.SubElement(
                    definitions,
                    _q(self.BPMN_NS, "process"),
                    {"id": process_id, "name": pool.text or title, "isExecutable": "false"},
                )
                self._fill_process(process, diagram, active_nodes, active_edges, parent_map, pool.id, lanes, lane_process_map)
        else:
            process_id = "Process_1"
            process = ET.SubElement(definitions, _q(self.BPMN_NS, "process"), {"id": process_id, "name": title, "isExecutable": "false"})
            self._fill_process(process, diagram, active_nodes, active_edges, parent_map, None, lanes, lane_process_map)

        if collaboration is not None:
            self._append_message_flows(collaboration, active_edges)
        self._append_diagram(definitions, diagram, active_nodes, active_edges, plane_element)
        return definitions

    def _fill_process(
        self,
        process: ET.Element,
        diagram: DiagramDocument,
        active_nodes: list[DiagramNode],
        active_edges: list[DiagramEdge],
        parent_map: dict[str, str | None],
        pool_id: str | None,
        lanes: list[DiagramNode],
        lane_process_map: dict[str, str],
    ) -> None:
        pool_lanes = [lane for lane in lanes if parent_map.get(lane.id) == pool_id]
        if pool_lanes:
            lane_set = ET.SubElement(process, _q(self.BPMN_NS, "laneSet"), {"id": f"LaneSet_{process.attrib['id']}"})
            for lane in pool_lanes:
                lane_el = ET.SubElement(lane_set, _q(self.BPMN_NS, "lane"), {"id": lane.id, "name": lane.text or lane.id})
                lane_process_map[lane.id] = process.attrib["id"]
                for node in active_nodes:
                    if parent_map.get(node.id) == lane.id and _coerce_node_type(node.node_type) != NodeType.LANE:
                        ET.SubElement(lane_el, _q(self.BPMN_NS, "flowNodeRef")).text = node.id

        for node in active_nodes:
            node_type = _coerce_node_type(node.node_type)
            if node_type in {NodeType.POOL, NodeType.LANE}:
                continue
            parent_id = parent_map.get(node.id)
            if pool_id is not None:
                if parent_id in {pool_id, None}:
                    pass
                elif not any(parent_id == lane.id for lane in pool_lanes):
                    continue
            export_type = node_type
            if node_type == NodeType.BOUNDARY_EVENT:
                attached_to = str((node.metadata or {}).get("attached_to") or "").strip()
                if not attached_to:
                    export_type = NodeType.INTERMEDIATE_EVENT
            element = ET.SubElement(process, _q(self.BPMN_NS, _bpmn_tag_for_node(export_type)), {"id": node.id})
            if node_type == NodeType.BOUNDARY_EVENT:
                attached_to = str((node.metadata or {}).get("attached_to") or "").strip()
                if attached_to:
                    element.set("attachedToRef", attached_to)
            if node_type == NodeType.ANNOTATION:
                if node.text:
                    text_element = ET.SubElement(element, _q(self.BPMN_NS, "text"))
                    text_element.text = node.text
            elif node.text:
                element.set("name", node.text)
            if node_type == NodeType.COLLAPSED_SUBPROCESS:
                element.set("triggeredByEvent", "false")

        for edge in active_edges:
            edge_type = _coerce_edge_type(edge.edge_type)
            if edge_type != EdgeType.SEQUENCE_FLOW:
                continue
            source = diagram.find_node(edge.source_id)
            target = diagram.find_node(edge.target_id)
            if not source or not target or source.deleted or target.deleted:
                continue
            if pool_id is not None:
                if not self._belongs_to_process(source.id, pool_id, parent_map, pool_lanes):
                    continue
                if not self._belongs_to_process(target.id, pool_id, parent_map, pool_lanes):
                    continue
            attrs = {"id": edge.id, "sourceRef": edge.source_id, "targetRef": edge.target_id}
            if edge.text:
                attrs["name"] = edge.text
            ET.SubElement(process, _q(self.BPMN_NS, "sequenceFlow"), attrs)

        for edge in active_edges:
            edge_type = _coerce_edge_type(edge.edge_type)
            if edge_type != EdgeType.ASSOCIATION:
                continue
            source = diagram.find_node(edge.source_id)
            target = diagram.find_node(edge.target_id)
            if not source or not target or source.deleted or target.deleted:
                continue
            if pool_id is not None:
                if not self._belongs_to_process(source.id, pool_id, parent_map, pool_lanes):
                    continue
                if not self._belongs_to_process(target.id, pool_id, parent_map, pool_lanes):
                    continue
            attrs = {"id": edge.id, "sourceRef": edge.source_id, "targetRef": edge.target_id}
            if edge.text:
                attrs["name"] = edge.text
            ET.SubElement(process, _q(self.BPMN_NS, "association"), attrs)

    def _belongs_to_process(
        self, node_id: str, pool_id: str, parent_map: dict[str, str | None], pool_lanes: list[DiagramNode]
    ) -> bool:
        parent_id = parent_map.get(node_id)
        if parent_id == pool_id:
            return True
        if parent_id is None:
            return False
        return any(parent_id == lane.id for lane in pool_lanes)

    def _append_message_flows(self, collaboration: ET.Element, active_edges: list[DiagramEdge]) -> None:
        for edge in active_edges:
            edge_type = _coerce_edge_type(edge.edge_type)
            if edge_type != EdgeType.MESSAGE_FLOW:
                continue
            if edge.source_id == edge.target_id:
                continue
            attrs = {"id": edge.id, "sourceRef": edge.source_id, "targetRef": edge.target_id}
            if edge.text:
                attrs["name"] = edge.text
            ET.SubElement(collaboration, _q(self.BPMN_NS, "messageFlow"), attrs)

    def _append_diagram(
        self,
        definitions: ET.Element,
        diagram: DiagramDocument,
        active_nodes: list[DiagramNode],
        active_edges: list[DiagramEdge],
        plane_element: str,
    ) -> None:
        bpmndiagram = ET.SubElement(definitions, _q(self.BPMNDI_NS, "BPMNDiagram"), {"id": "BPMNDiagram_1"})
        plane = ET.SubElement(
            bpmndiagram,
            _q(self.BPMNDI_NS, "BPMNPlane"),
            {"id": "BPMNPlane_1", "bpmnElement": plane_element},
        )

        for node in active_nodes:
            node_type = _coerce_node_type(node.node_type)
            attrs = {"id": f"{node.id}_di", "bpmnElement": node.id}
            if node_type in {NodeType.POOL, NodeType.LANE}:
                attrs["isHorizontal"] = "true"
            if node_type == NodeType.COLLAPSED_SUBPROCESS:
                attrs["isExpanded"] = "false"
            shape = ET.SubElement(plane, _q(self.BPMNDI_NS, "BPMNShape"), attrs)
            ET.SubElement(
                shape,
                _q(self.DC_NS, "Bounds"),
                {
                    "x": _fmt(node.x),
                    "y": _fmt(node.y),
                    "width": _fmt(node.width),
                    "height": _fmt(node.height),
                },
            )
            label_bounds = _label_bounds_for_node(node)
            if label_bounds is not None:
                label = ET.SubElement(shape, _q(self.BPMNDI_NS, "BPMNLabel"))
                ET.SubElement(
                    label,
                    _q(self.DC_NS, "Bounds"),
                    {
                        "x": _fmt(label_bounds["x"]),
                        "y": _fmt(label_bounds["y"]),
                        "width": _fmt(label_bounds["width"]),
                        "height": _fmt(label_bounds["height"]),
                    },
                )

        for edge in active_edges:
            source = diagram.find_node(edge.source_id)
            target = diagram.find_node(edge.target_id)
            if not source or not target or source.deleted or target.deleted:
                continue
            edge_el = ET.SubElement(plane, _q(self.BPMNDI_NS, "BPMNEdge"), {"id": f"{edge.id}_di", "bpmnElement": edge.id})
            points = edge.waypoints or [source.center, target.center]
            if len(points) < 2:
                points = [source.center, target.center]
            for point in points:
                ET.SubElement(edge_el, _q(self.DI_NS, "waypoint"), {"x": _fmt(point.x), "y": _fmt(point.y)})
            edge_label_bounds = _label_bounds_for_edge(edge, points)
            if edge_label_bounds is not None:
                label = ET.SubElement(edge_el, _q(self.BPMNDI_NS, "BPMNLabel"))
                ET.SubElement(
                    label,
                    _q(self.DC_NS, "Bounds"),
                    {
                        "x": _fmt(edge_label_bounds["x"]),
                        "y": _fmt(edge_label_bounds["y"]),
                        "width": _fmt(edge_label_bounds["width"]),
                        "height": _fmt(edge_label_bounds["height"]),
                    },
                )

    def _normalize_with_open_bpmn(self, output_path: Path) -> None:
        if not self.settings.open_bpmn_jar:
            return
        jar = Path(self.settings.open_bpmn_jar)
        if not jar.exists():
            return
        command = [self.settings.open_bpmn_java, "-jar", str(jar), str(output_path)]
        try:
            subprocess.run(command, capture_output=True, timeout=60, check=False)
        except Exception:
            return


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _bpmn_tag_for_node(node_type: NodeType) -> str:
    mapping = {
        NodeType.TASK: "task",
        NodeType.USER_TASK: "userTask",
        NodeType.SERVICE_TASK: "serviceTask",
        NodeType.SUBPROCESS: "subProcess",
        NodeType.COLLAPSED_SUBPROCESS: "subProcess",
        NodeType.START_EVENT: "startEvent",
        NodeType.INTERMEDIATE_EVENT: "intermediateCatchEvent",
        NodeType.END_EVENT: "endEvent",
        NodeType.BOUNDARY_EVENT: "boundaryEvent",
        NodeType.EXCLUSIVE_GATEWAY: "exclusiveGateway",
        NodeType.PARALLEL_GATEWAY: "parallelGateway",
        NodeType.INCLUSIVE_GATEWAY: "inclusiveGateway",
        NodeType.EVENT_BASED_GATEWAY: "eventBasedGateway",
        NodeType.DATA_OBJECT: "dataObjectReference",
        NodeType.DATA_STORE: "dataStoreReference",
        NodeType.ANNOTATION: "textAnnotation",
    }
    return mapping.get(node_type, "task")


def _extract_node_text(element: ET.Element, node_type: NodeType) -> str:
    if node_type == NodeType.ANNOTATION:
        text_element = element.find(_q(BPMNSemanticExporter.BPMN_NS, "text"))
        return (text_element.text or "").strip() if text_element is not None else ""
    return element.get("name", "")


def _label_bounds_for_node(node: DiagramNode) -> dict[str, float] | None:
    if not node.text.strip():
        return None
    node_type = _coerce_node_type(node.node_type)
    if node_type == NodeType.POOL:
        return {
            "x": node.x,
            "y": node.y,
            "width": min(42.0, max(28.0, node.width * 0.12)),
            "height": node.height,
        }
    if node_type == NodeType.LANE:
        return {
            "x": node.x,
            "y": node.y,
            "width": min(42.0, max(28.0, node.width * 0.12)),
            "height": node.height,
        }
    if node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION}:
        return {
            "x": node.x,
            "y": node.y + node.height + 8.0,
            "width": max(node.width, 120.0),
            "height": max(40.0, min(120.0, node.height * 0.8)),
        }
    if node_type in {
        NodeType.TASK,
        NodeType.USER_TASK,
        NodeType.SERVICE_TASK,
        NodeType.SUBPROCESS,
        NodeType.COLLAPSED_SUBPROCESS,
    }:
        return {
            "x": node.x + 8.0,
            "y": node.y + 8.0,
            "width": max(node.width - 16.0, 40.0),
            "height": max(node.height - 16.0, 24.0),
        }
    return None


def _label_bounds_for_edge(edge: DiagramEdge, points: list) -> dict[str, float] | None:
    if not edge.text.strip() or len(points) < 2:
        return None
    mid_index = len(points) // 2
    point = points[mid_index]
    return {
        "x": float(point.x) - 90.0,
        "y": float(point.y) - 18.0,
        "width": 180.0,
        "height": 36.0,
    }


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


def parse_bpmn_semantics(bpmn_path: Path) -> dict:
    tree = ET.parse(bpmn_path)
    root = tree.getroot()
    bpmn_ns = BPMNSemanticExporter.BPMN_NS
    bpmndi_ns = BPMNSemanticExporter.BPMNDI_NS
    dc_ns = BPMNSemanticExporter.DC_NS
    di_ns = BPMNSemanticExporter.DI_NS
    ns = {
        "bpmn": bpmn_ns,
        "bpmndi": bpmndi_ns,
        "dc": dc_ns,
        "di": di_ns,
    }

    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    parent_map: dict[str, str | None] = {}

    process_participant_map: dict[str, str] = {}
    for participant in root.findall(".//bpmn:participant", ns):
        participant_id = participant.get("id")
        if not participant_id:
            continue
        process_ref = participant.get("processRef")
        if process_ref:
            process_participant_map[process_ref] = participant_id
        nodes[participant_id] = {
            "id": participant_id,
            "type": NodeType.POOL,
            "text": participant.get("name", ""),
            "parent_id": None,
        }

    for lane in root.findall(".//bpmn:lane", ns):
        lane_id = lane.get("id")
        if not lane_id:
            continue
        nodes[lane_id] = {
            "id": lane_id,
            "type": NodeType.LANE,
            "text": lane.get("name", ""),
            "parent_id": _parent_participant_for_lane(root, lane_id, ns),
        }
        parent_map[lane_id] = nodes[lane_id]["parent_id"]
        for flow_node_ref in lane.findall("bpmn:flowNodeRef", ns):
            if flow_node_ref.text:
                parent_map[flow_node_ref.text] = lane_id

    flow_node_tags = {
        "task": NodeType.TASK,
        "userTask": NodeType.USER_TASK,
        "serviceTask": NodeType.SERVICE_TASK,
        "subProcess": NodeType.SUBPROCESS,
        "startEvent": NodeType.START_EVENT,
        "intermediateCatchEvent": NodeType.INTERMEDIATE_EVENT,
        "endEvent": NodeType.END_EVENT,
        "boundaryEvent": NodeType.BOUNDARY_EVENT,
        "exclusiveGateway": NodeType.EXCLUSIVE_GATEWAY,
        "parallelGateway": NodeType.PARALLEL_GATEWAY,
        "inclusiveGateway": NodeType.INCLUSIVE_GATEWAY,
        "eventBasedGateway": NodeType.EVENT_BASED_GATEWAY,
        "dataObjectReference": NodeType.DATA_OBJECT,
        "dataStoreReference": NodeType.DATA_STORE,
    }
    for process in root.findall(".//bpmn:process", ns):
        process_id = process.get("id", "")
        pool_parent_id = process_participant_map.get(process_id)
        for tag_name, node_type in flow_node_tags.items():
            for element in process.findall(f".//bpmn:{tag_name}", ns):
                node_id = element.get("id")
                if not node_id:
                    continue
                resolved_type = node_type
                if node_type == NodeType.SUBPROCESS:
                    if element.get("triggeredByEvent") == "false":
                        resolved_type = NodeType.COLLAPSED_SUBPROCESS
                nodes[node_id] = {
                    "id": node_id,
                    "type": resolved_type,
                    "text": _extract_node_text(element, resolved_type),
                    "parent_id": parent_map.get(node_id) or pool_parent_id,
                    "metadata": {
                        "attached_to": element.get("attachedToRef")
                    } if resolved_type == NodeType.BOUNDARY_EVENT and element.get("attachedToRef") else {},
                }

    for annotation in root.findall(".//bpmn:textAnnotation", ns):
        annotation_id = annotation.get("id")
        if not annotation_id or annotation_id in nodes:
            continue
        nodes[annotation_id] = {
            "id": annotation_id,
            "type": NodeType.ANNOTATION,
            "text": _extract_node_text(annotation, NodeType.ANNOTATION),
            "parent_id": parent_map.get(annotation_id),
        }

    for flow in root.findall(".//bpmn:sequenceFlow", ns):
        flow_id = flow.get("id")
        if flow_id:
            edges[flow_id] = {
                "id": flow_id,
                "type": EdgeType.SEQUENCE_FLOW,
                "source_id": flow.get("sourceRef", ""),
                "target_id": flow.get("targetRef", ""),
                "text": flow.get("name", ""),
            }

    for flow in root.findall(".//bpmn:messageFlow", ns):
        flow_id = flow.get("id")
        if flow_id:
            edges[flow_id] = {
                "id": flow_id,
                "type": EdgeType.MESSAGE_FLOW,
                "source_id": flow.get("sourceRef", ""),
                "target_id": flow.get("targetRef", ""),
                "text": flow.get("name", ""),
            }

    for flow in root.findall(".//bpmn:association", ns):
        flow_id = flow.get("id")
        if flow_id:
            edges[flow_id] = {
                "id": flow_id,
                "type": EdgeType.ASSOCIATION,
                "source_id": flow.get("sourceRef", ""),
                "target_id": flow.get("targetRef", ""),
                "text": flow.get("name", ""),
            }

    max_x = 0.0
    max_y = 0.0
    for shape in root.findall(".//bpmndi:BPMNShape", ns):
        element_id = shape.get("bpmnElement")
        bounds = shape.find("dc:Bounds", ns)
        if not element_id or bounds is None or element_id not in nodes:
            continue
        x = float(bounds.get("x", "0"))
        y = float(bounds.get("y", "0"))
        width = float(bounds.get("width", "10"))
        height = float(bounds.get("height", "10"))
        nodes[element_id].update({"x": x, "y": y, "width": width, "height": height})
        max_x = max(max_x, x + width)
        max_y = max(max_y, y + height)

    for edge in root.findall(".//bpmndi:BPMNEdge", ns):
        element_id = edge.get("bpmnElement")
        if not element_id or element_id not in edges:
            continue
        waypoints = []
        for waypoint in edge.findall("di:waypoint", ns):
            waypoints.append(
                {
                    "x": float(waypoint.get("x", "0")),
                    "y": float(waypoint.get("y", "0")),
                }
            )
            max_x = max(max_x, float(waypoint.get("x", "0")))
            max_y = max(max_y, float(waypoint.get("y", "0")))
        edges[element_id]["waypoints"] = waypoints

    return {
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "image_width": int(max(max_x, 1.0)),
        "image_height": int(max(max_y, 1.0)),
    }


def _parent_participant_for_lane(root: ET.Element, lane_id: str, ns: dict[str, str]) -> str | None:
    for participant in root.findall(".//bpmn:participant", ns):
        process_ref = participant.get("processRef")
        if not process_ref:
            continue
        process = root.find(f".//bpmn:process[@id='{process_ref}']", ns)
        if process is None:
            continue
        lane = process.find(f".//bpmn:lane[@id='{lane_id}']", ns)
        if lane is not None:
            return participant.get("id")
    return None
