from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class NodeType(str, Enum):
    POOL = "pool"
    LANE = "lane"
    TASK = "task"
    USER_TASK = "user_task"
    SERVICE_TASK = "service_task"
    SUBPROCESS = "subprocess"
    COLLAPSED_SUBPROCESS = "collapsed_subprocess"
    START_EVENT = "start_event"
    INTERMEDIATE_EVENT = "intermediate_event"
    END_EVENT = "end_event"
    BOUNDARY_EVENT = "boundary_event"
    EXCLUSIVE_GATEWAY = "exclusive_gateway"
    PARALLEL_GATEWAY = "parallel_gateway"
    INCLUSIVE_GATEWAY = "inclusive_gateway"
    EVENT_BASED_GATEWAY = "event_based_gateway"
    DATA_OBJECT = "data_object"
    DATA_STORE = "data_store"
    ANNOTATION = "annotation"


class EdgeType(str, Enum):
    SEQUENCE_FLOW = "sequence_flow"
    MESSAGE_FLOW = "message_flow"
    ASSOCIATION = "association"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


NODE_LABELS = {
    NodeType.POOL: "Pool",
    NodeType.LANE: "Lane",
    NodeType.TASK: "Tarea",
    NodeType.USER_TASK: "Tarea de usuario",
    NodeType.SERVICE_TASK: "Tarea de servicio",
    NodeType.SUBPROCESS: "Subproceso",
    NodeType.COLLAPSED_SUBPROCESS: "Subproceso colapsado",
    NodeType.START_EVENT: "Evento de inicio",
    NodeType.INTERMEDIATE_EVENT: "Evento intermedio",
    NodeType.END_EVENT: "Evento de fin",
    NodeType.BOUNDARY_EVENT: "Evento de borde",
    NodeType.EXCLUSIVE_GATEWAY: "Gateway exclusivo",
    NodeType.PARALLEL_GATEWAY: "Gateway paralelo",
    NodeType.INCLUSIVE_GATEWAY: "Gateway inclusivo",
    NodeType.EVENT_BASED_GATEWAY: "Gateway basado en eventos",
    NodeType.DATA_OBJECT: "Objeto de datos",
    NodeType.DATA_STORE: "Deposito de datos",
    NodeType.ANNOTATION: "Anotacion",
}

EDGE_LABELS = {
    EdgeType.SEQUENCE_FLOW: "Flujo de secuencia",
    EdgeType.MESSAGE_FLOW: "Flujo de mensaje",
    EdgeType.ASSOCIATION: "Asociacion",
}

EVENT_NODE_TYPES = {
    NodeType.START_EVENT,
    NodeType.INTERMEDIATE_EVENT,
    NodeType.END_EVENT,
    NodeType.BOUNDARY_EVENT,
}


@dataclass
class Point:
    x: float
    y: float


@dataclass
class ReviewIssue:
    id: str
    severity: IssueSeverity
    message: str
    related_kind: str | None = None
    related_id: str | None = None
    resolved: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagramNode:
    id: str
    node_type: NodeType
    x: float
    y: float
    width: float
    height: float
    text: str = ""
    confidence: float = 0.0
    deleted: bool = False
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2.0, self.y + self.height / 2.0)


@dataclass
class DiagramEdge:
    id: str
    edge_type: EdgeType
    source_id: str
    target_id: str
    confidence: float = 0.0
    text: str = ""
    deleted: bool = False
    waypoints: list[Point] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiagramDocument:
    source_pdf: Path
    source_image: Path
    image_width: int
    image_height: int
    nodes: list[DiagramNode] = field(default_factory=list)
    edges: list[DiagramEdge] = field(default_factory=list)
    issues: list[ReviewIssue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def unresolved_issues(self) -> list[ReviewIssue]:
        return [issue for issue in self.issues if not issue.resolved]

    def active_nodes(self) -> list[DiagramNode]:
        return [node for node in self.nodes if not node.deleted]

    def active_edges(self) -> list[DiagramEdge]:
        return [edge for edge in self.edges if not edge.deleted]

    def find_node(self, node_id: str) -> DiagramNode | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def find_edge(self, edge_id: str) -> DiagramEdge | None:
        for edge in self.edges:
            if edge.id == edge_id:
                return edge
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_pdf": str(self.source_pdf),
            "source_image": str(self.source_image),
            "image_width": self.image_width,
            "image_height": self.image_height,
            "nodes": [_serialize_dataclass(node) for node in self.nodes],
            "edges": [_serialize_dataclass(edge) for edge in self.edges],
            "issues": [_serialize_dataclass(issue) for issue in self.issues],
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramDocument":
        return cls(
            source_pdf=_deserialize_path(data["source_pdf"]),
            source_image=_deserialize_path(data["source_image"]),
            image_width=int(data["image_width"]),
            image_height=int(data["image_height"]),
            nodes=[
                DiagramNode(
                    id=node["id"],
                    node_type=NodeType(node["node_type"]),
                    x=float(node["x"]),
                    y=float(node["y"]),
                    width=float(node["width"]),
                    height=float(node["height"]),
                    text=node.get("text", ""),
                    confidence=float(node.get("confidence", 0.0)),
                    deleted=bool(node.get("deleted", False)),
                    parent_id=node.get("parent_id"),
                    metadata=node.get("metadata", {}),
                )
                for node in data.get("nodes", [])
            ],
            edges=[
                DiagramEdge(
                    id=edge["id"],
                    edge_type=EdgeType(edge["edge_type"]),
                    source_id=edge["source_id"],
                    target_id=edge["target_id"],
                    confidence=float(edge.get("confidence", 0.0)),
                    text=edge.get("text", ""),
                    deleted=bool(edge.get("deleted", False)),
                    waypoints=[
                        Point(x=float(point["x"]), y=float(point["y"]))
                        for point in edge.get("waypoints", [])
                    ],
                    metadata=edge.get("metadata", {}),
                )
                for edge in data.get("edges", [])
            ],
            issues=[
                ReviewIssue(
                    id=issue["id"],
                    severity=IssueSeverity(issue["severity"]),
                    message=issue["message"],
                    related_kind=issue.get("related_kind"),
                    related_id=issue.get("related_id"),
                    resolved=bool(issue.get("resolved", False)),
                    metadata=issue.get("metadata", {}),
                )
                for issue in data.get("issues", [])
            ],
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, value: str) -> "DiagramDocument":
        return cls.from_dict(json.loads(value))


def _serialize_dataclass(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    for key, item in list(payload.items()):
        if isinstance(item, Enum):
            payload[key] = item.value
    return payload


def _deserialize_path(value: str) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value))).resolve()
    if expanded.exists():
        return expanded
    legacy = Path(value).expanduser().resolve()
    if legacy.exists():
        return legacy
    return expanded


def normalize_event_node_size(node: DiagramNode) -> None:
    if node.node_type not in EVENT_NODE_TYPES:
        return
    size = float(max(18.0, min(node.width, node.height)))
    center = node.center
    node.width = size
    node.height = size
    node.x = float(center.x - (size / 2.0))
    node.y = float(center.y - (size / 2.0))


def normalize_event_nodes(nodes: list[DiagramNode]) -> None:
    for node in nodes:
        normalize_event_node_size(node)
