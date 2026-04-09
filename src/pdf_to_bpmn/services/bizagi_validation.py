from __future__ import annotations

import uuid

from pdf_to_bpmn.domain import DiagramDocument, DiagramEdge, DiagramNode, EdgeType, IssueSeverity, NodeType, ReviewIssue


class BizagiStrictValidator:
    PROFILE = "bizagi-strict"

    def validate(self, diagram: DiagramDocument) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        nodes_by_id = {node.id: node for node in diagram.nodes if not node.deleted}
        pools = [node for node in nodes_by_id.values() if node.node_type == NodeType.POOL]
        lanes = [node for node in nodes_by_id.values() if node.node_type == NodeType.LANE]

        self._validate_ids(diagram, issues)
        self._validate_containers(nodes_by_id, pools, lanes, issues)
        self._validate_edges(diagram, nodes_by_id, issues)
        self._validate_boundary_events(nodes_by_id, issues)
        return issues

    def sync_issues(self, diagram: DiagramDocument) -> list[ReviewIssue]:
        diagram.issues = [
            issue for issue in diagram.issues
            if issue.metadata.get("profile") != self.PROFILE
        ]
        issues = self.validate(diagram)
        diagram.issues.extend(issues)
        return issues

    def _validate_ids(self, diagram: DiagramDocument, issues: list[ReviewIssue]) -> None:
        seen: set[str] = set()
        for node in diagram.nodes:
            if node.deleted:
                continue
            if node.id in seen:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    f"ID duplicado de nodo para Bizagi: {node.id}.",
                    "node",
                    node.id,
                ))
            seen.add(node.id)
        for edge in diagram.edges:
            if edge.deleted:
                continue
            if edge.id in seen:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    f"ID duplicado de edge para Bizagi: {edge.id}.",
                    "edge",
                    edge.id,
                ))
            seen.add(edge.id)

    def _validate_containers(
        self,
        nodes_by_id: dict[str, DiagramNode],
        pools: list[DiagramNode],
        lanes: list[DiagramNode],
        issues: list[ReviewIssue],
    ) -> None:
        has_collaboration = bool(pools)
        for lane in lanes:
            if not lane.parent_id or lane.parent_id not in nodes_by_id:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    "La lane no pertenece a un pool valido; Bizagi puede desestructurar la colaboracion.",
                    "node",
                    lane.id,
                ))
                continue
            parent = nodes_by_id[lane.parent_id]
            if parent.node_type != NodeType.POOL:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    "La lane no esta contenida directamente por un pool.",
                    "node",
                    lane.id,
                ))

        if has_collaboration:
            for node in nodes_by_id.values():
                if node.node_type in {NodeType.POOL, NodeType.LANE}:
                    continue
                if node.node_type == NodeType.BOUNDARY_EVENT:
                    continue
                if not self._pool_owner(node, nodes_by_id):
                    issues.append(self._issue(
                        IssueSeverity.ERROR,
                        "El nodo no pertenece a ningun pool; Bizagi puede importarlo fuera de proceso.",
                        "node",
                        node.id,
                    ))

    def _validate_edges(
        self,
        diagram: DiagramDocument,
        nodes_by_id: dict[str, DiagramNode],
        issues: list[ReviewIssue],
    ) -> None:
        for edge in diagram.edges:
            if edge.deleted:
                continue
            source = nodes_by_id.get(edge.source_id)
            target = nodes_by_id.get(edge.target_id)
            if source is None or target is None:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    "El edge referencia nodos inexistentes; Bizagi no podra importarlo bien.",
                    "edge",
                    edge.id,
                ))
                continue
            source_pool = self._pool_owner(source, nodes_by_id)
            target_pool = self._pool_owner(target, nodes_by_id)
            if edge.edge_type == EdgeType.SEQUENCE_FLOW:
                if source_pool and target_pool and source_pool.id != target_pool.id:
                    issues.append(self._issue(
                        IssueSeverity.ERROR,
                        "El sequence flow cruza pools; Bizagi espera message flow entre participantes.",
                        "edge",
                        edge.id,
                    ))
                if source.node_type in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION} or target.node_type in {
                    NodeType.DATA_OBJECT,
                    NodeType.DATA_STORE,
                    NodeType.ANNOTATION,
                }:
                    issues.append(self._issue(
                        IssueSeverity.ERROR,
                        "El sequence flow conecta artefactos; Bizagi lo importara de forma semantica incorrecta.",
                        "edge",
                        edge.id,
                    ))
            elif edge.edge_type == EdgeType.MESSAGE_FLOW:
                if not source_pool or not target_pool or source_pool.id == target_pool.id:
                    issues.append(self._issue(
                        IssueSeverity.ERROR,
                        "El message flow no conecta pools distintos; Bizagi suele rechazar o reinterpretar este caso.",
                        "edge",
                        edge.id,
                    ))
            elif edge.edge_type == EdgeType.ASSOCIATION:
                if source.node_type not in {NodeType.DATA_OBJECT, NodeType.DATA_STORE, NodeType.ANNOTATION} and target.node_type not in {
                    NodeType.DATA_OBJECT,
                    NodeType.DATA_STORE,
                    NodeType.ANNOTATION,
                }:
                    issues.append(self._issue(
                        IssueSeverity.WARNING,
                        "La association no toca artefactos; Bizagi podria requerir revisión manual.",
                        "edge",
                        edge.id,
                    ))
            if len(edge.waypoints) < 2:
                issues.append(self._issue(
                    IssueSeverity.WARNING,
                    "El edge no tiene waypoints suficientes; Bizagi podria relayoutarlo agresivamente.",
                    "edge",
                    edge.id,
                ))

    def _validate_boundary_events(self, nodes_by_id: dict[str, DiagramNode], issues: list[ReviewIssue]) -> None:
        for node in nodes_by_id.values():
            if node.node_type != NodeType.BOUNDARY_EVENT:
                continue
            attached_to = str((node.metadata or {}).get("attached_to") or "").strip()
            if not attached_to:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    "El boundary event no tiene attached_to; Bizagi necesita attachedToRef consistente.",
                    "node",
                    node.id,
                ))
                continue
            owner = nodes_by_id.get(attached_to)
            if owner is None or owner.node_type not in {
                NodeType.TASK,
                NodeType.USER_TASK,
                NodeType.SERVICE_TASK,
                NodeType.SUBPROCESS,
                NodeType.COLLAPSED_SUBPROCESS,
            }:
                issues.append(self._issue(
                    IssueSeverity.ERROR,
                    "El boundary event apunta a una actividad invalida para Bizagi.",
                    "node",
                    node.id,
                ))

    def _pool_owner(self, node: DiagramNode, nodes_by_id: dict[str, DiagramNode]) -> DiagramNode | None:
        current = node
        visited: set[str] = set()
        while current.parent_id and current.parent_id not in visited:
            visited.add(current.parent_id)
            parent = nodes_by_id.get(current.parent_id)
            if parent is None:
                return None
            if parent.node_type == NodeType.POOL:
                return parent
            current = parent
        return None

    def _issue(
        self,
        severity: IssueSeverity,
        message: str,
        related_kind: str | None,
        related_id: str | None,
    ) -> ReviewIssue:
        return ReviewIssue(
            id=f"issue-{uuid.uuid4().hex[:8]}",
            severity=severity,
            message=message,
            related_kind=related_kind,
            related_id=related_id,
            metadata={"profile": self.PROFILE},
        )
