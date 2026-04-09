from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import DiagramDocument, EdgeType, NodeType
from pdf_to_bpmn.services.analysis import _coerce_edge_type, _coerce_node_type
from pdf_to_bpmn.services.bpmn_semantic import parse_bpmn_semantics


MASTER_ALIASES = {
    NodeType.POOL: ["Pool", "Grupo", "Participante", "Pool / Lane", "Grupo / Calle"],
    NodeType.LANE: ["Lane", "Carril", "Franja", "Pool / Lane", "Grupo / Calle"],
    NodeType.TASK: ["Task", "Tarea"],
    NodeType.USER_TASK: ["User Task", "Tarea de usuario"],
    NodeType.SERVICE_TASK: ["Service Task", "Tarea de servicio"],
    NodeType.SUBPROCESS: ["Sub-Process", "Subprocess", "Subproceso", "Expanded Sub-Process", "Subproceso expandido"],
    NodeType.COLLAPSED_SUBPROCESS: [
        "Collapsed Sub-Process",
        "Subproceso colapsado",
        "Subproceso contraído",
    ],
    NodeType.START_EVENT: ["Start Event", "Evento de inicio"],
    NodeType.INTERMEDIATE_EVENT: ["Intermediate Event", "Evento intermedio"],
    NodeType.END_EVENT: ["End Event", "Evento de fin", "Evento final", "Evento de finalización"],
    NodeType.BOUNDARY_EVENT: ["Boundary Event", "Evento de borde", "Evento limite"],
    NodeType.EXCLUSIVE_GATEWAY: ["Exclusive Gateway", "Gateway exclusivo", "Compuerta exclusiva", "Gateway", "Puerta de enlace"],
    NodeType.PARALLEL_GATEWAY: ["Parallel Gateway", "Gateway paralelo", "Compuerta paralela", "Gateway", "Puerta de enlace"],
    NodeType.INCLUSIVE_GATEWAY: ["Inclusive Gateway", "Gateway inclusivo", "Compuerta inclusiva", "Gateway", "Puerta de enlace"],
    NodeType.EVENT_BASED_GATEWAY: [
        "Event-Based Gateway",
        "Gateway basado en eventos",
        "Compuerta basada en eventos",
        "Gateway",
        "Puerta de enlace",
    ],
    NodeType.DATA_OBJECT: ["Data Object", "Objeto de datos"],
    NodeType.DATA_STORE: ["Data Store", "Almacén de datos", "Deposito de datos", "Depósito de datos"],
    NodeType.ANNOTATION: ["Text Annotation", "Anotacion de texto", "Anotación de texto"],
    EdgeType.SEQUENCE_FLOW: ["Sequence Flow", "Flujo de secuencia"],
    EdgeType.MESSAGE_FLOW: ["Message Flow", "Flujo de mensaje", "Flujo de mensajes"],
    EdgeType.ASSOCIATION: ["Association", "Asociacion", "Asociación"],
}

MASTER_FALLBACKS = {
    NodeType.USER_TASK: [NodeType.TASK],
    NodeType.SERVICE_TASK: [NodeType.TASK],
    NodeType.COLLAPSED_SUBPROCESS: [NodeType.SUBPROCESS, NodeType.TASK],
    NodeType.SUBPROCESS: [NodeType.TASK],
}


class VisioExporter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def inspect_installation(self, output_path: Path) -> Path:
        payload = {"output_path": _to_windows_path(output_path)}
        script = _inventory_script(self.settings)
        stdout, stderr = self._run_powershell_payload(script, payload)
        if not output_path.exists():
            raise RuntimeError(
                f"Visio no genero el inventario esperado en {output_path}. "
                "Revisa hints de template/stencil o permisos de Visio.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
        return output_path

    def export(self, diagram: DiagramDocument, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_payload(diagram, output_path)
        script = _export_script(self.settings)
        stdout, stderr = self._run_powershell_payload(script, payload)
        if not output_path.exists():
            raise RuntimeError(
                f"Visio no genero el archivo esperado en {output_path}. "
                "Revisa los masters BPMN disponibles con inspect-visio.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
        return output_path

    def export_from_bpmn(self, bpmn_path: Path, output_path: Path, diagram: DiagramDocument | None = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_payload_from_bpmn(bpmn_path, output_path)
        if not payload.get("nodes") and diagram is not None:
            payload = self._build_payload(diagram, output_path)
        script = _export_script(self.settings)
        stdout, stderr = self._run_powershell_payload(script, payload)
        if not output_path.exists():
            raise RuntimeError(
                f"Visio no genero el archivo esperado en {output_path}. "
                "Revisa el BPMN semantico generado y los masters BPMN disponibles.\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
        return output_path

    def _build_payload(self, diagram: DiagramDocument, output_path: Path) -> dict:
        page_width_in = max(diagram.image_width / 120.0, 8.0)
        page_height_in = max(diagram.image_height / 120.0, 6.0)
        parent_map = _infer_export_parent_map(diagram)

        def to_visio(node) -> dict:
            node_type = _coerce_node_type(node.node_type)
            pin_x = (node.x + node.width / 2.0) / diagram.image_width * page_width_in
            pin_y = page_height_in - (
                (node.y + node.height / 2.0) / diagram.image_height * page_height_in
            )
            width_in = max(node.width / diagram.image_width * page_width_in, 0.2)
            height_in = max(node.height / diagram.image_height * page_height_in, 0.2)
            return {
                "id": node.id,
                "type": node_type.value,
                "aliases": MASTER_ALIASES[node_type],
                "fallback_aliases": _fallback_aliases_for(node_type),
                "parent_id": parent_map.get(node.id),
                "text": node.text,
                "deleted": node.deleted,
                "pin_x": pin_x,
                "pin_y": pin_y,
                "width_in": width_in,
                "height_in": height_in,
                "is_container": node_type in {NodeType.POOL, NodeType.LANE},
                "sort_key": _node_sort_key(node_type),
            }

        def edge_payload(edge) -> dict:
            edge_type = _coerce_edge_type(edge.edge_type)
            source = diagram.find_node(edge.source_id)
            target = diagram.find_node(edge.target_id)
            if not source or not target:
                return {}
            source_anchor, target_anchor = _edge_anchors(source, target)
            waypoints = [
                {
                    "x": point.x / diagram.image_width * page_width_in,
                    "y": page_height_in - (point.y / diagram.image_height * page_height_in),
                }
                for point in (edge.waypoints or [])
            ]
            return {
                "id": edge.id,
                "type": edge_type.value,
                "aliases": MASTER_ALIASES[edge_type],
                "fallback_aliases": [],
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "text": edge.text,
                "deleted": edge.deleted,
                "source_anchor_x": source_anchor[0],
                "source_anchor_y": source_anchor[1],
                "target_anchor_x": target_anchor[0],
                "target_anchor_y": target_anchor[1],
                "waypoints": waypoints,
            }

        return {
            "output_path": _to_windows_path(output_path),
            "log_path": _to_windows_path(output_path.with_suffix(".export.log.txt")),
            "page_width_in": page_width_in,
            "page_height_in": page_height_in,
            "keep_visio_open": self.settings.export_keep_visio_open,
            "template_hint": _to_windows_path(self.settings.visio_template_hint),
            "stencil_hint": _to_windows_path(self.settings.visio_stencil_hint),
            "nodes": [to_visio(node) for node in diagram.nodes],
            "edges": [
                item
                for item in (edge_payload(edge) for edge in diagram.edges)
                if item
            ],
        }

    def _build_payload_from_bpmn(self, bpmn_path: Path, output_path: Path) -> dict:
        semantic = parse_bpmn_semantics(bpmn_path)
        image_width = max(float(semantic.get("image_width") or 1.0), 1.0)
        image_height = max(float(semantic.get("image_height") or 1.0), 1.0)
        page_width_in = max(image_width / 120.0, 8.0)
        page_height_in = max(image_height / 120.0, 6.0)

        nodes = [node for node in semantic.get("nodes", []) if not node.get("deleted")]
        edges = [edge for edge in semantic.get("edges", []) if not edge.get("deleted")]
        node_map = {node["id"]: node for node in nodes if node.get("id")}

        def to_visio(node: dict) -> dict:
            node_type = _coerce_node_type(node.get("type"))
            x = float(node.get("x", 0.0))
            y = float(node.get("y", 0.0))
            width = max(float(node.get("width", 10.0)), 1.0)
            height = max(float(node.get("height", 10.0)), 1.0)
            pin_x = (x + width / 2.0) / image_width * page_width_in
            pin_y = page_height_in - ((y + height / 2.0) / image_height * page_height_in)
            width_in = max(width / image_width * page_width_in, 0.2)
            height_in = max(height / image_height * page_height_in, 0.2)
            return {
                "id": node["id"],
                "type": node_type.value,
                "aliases": MASTER_ALIASES[node_type],
                "fallback_aliases": _fallback_aliases_for(node_type),
                "parent_id": node.get("parent_id"),
                "text": node.get("text", ""),
                "deleted": False,
                "pin_x": pin_x,
                "pin_y": pin_y,
                "width_in": width_in,
                "height_in": height_in,
                "is_container": node_type in {NodeType.POOL, NodeType.LANE},
                "sort_key": _node_sort_key(node_type),
            }

        def edge_payload(edge: dict) -> dict:
            edge_type = _coerce_edge_type(edge.get("type"))
            source = node_map.get(edge.get("source_id"))
            target = node_map.get(edge.get("target_id"))
            if not source or not target:
                return {}
            source_anchor, target_anchor = _edge_anchors_from_bounds(source, target)
            waypoints = [
                {
                    "x": float(point.get("x", 0.0)) / image_width * page_width_in,
                    "y": page_height_in - (float(point.get("y", 0.0)) / image_height * page_height_in),
                }
                for point in edge.get("waypoints", [])
            ]
            return {
                "id": edge["id"],
                "type": edge_type.value,
                "aliases": MASTER_ALIASES[edge_type],
                "fallback_aliases": [],
                "source_id": edge.get("source_id"),
                "target_id": edge.get("target_id"),
                "text": edge.get("text", ""),
                "deleted": False,
                "source_anchor_x": source_anchor[0],
                "source_anchor_y": source_anchor[1],
                "target_anchor_x": target_anchor[0],
                "target_anchor_y": target_anchor[1],
                "waypoints": waypoints,
            }

        return {
            "output_path": _to_windows_path(output_path),
            "log_path": _to_windows_path(output_path.with_suffix(".export.log.txt")),
            "page_width_in": page_width_in,
            "page_height_in": page_height_in,
            "keep_visio_open": self.settings.export_keep_visio_open,
            "template_hint": _to_windows_path(self.settings.visio_template_hint),
            "stencil_hint": _to_windows_path(self.settings.visio_stencil_hint),
            "nodes": [to_visio(node) for node in nodes],
            "edges": [item for item in (edge_payload(edge) for edge in edges) if item],
        }

    def _run_powershell_payload(self, script: str, payload: dict) -> tuple[str, str]:
        temp_root = _powershell_temp_root()
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pdf2bpmn-", dir=temp_root) as temp_dir:
            temp_dir_path = Path(temp_dir)
            script_path = temp_dir_path / "action.ps1"
            payload_path = temp_dir_path / "payload.json"
            script_path.write_text(script, encoding="utf-8")
            payload_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log_path = Path(str(payload.get("log_path") or output_path_with_default(payload)))
            command = [
                self.settings.visio_powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _to_windows_path(script_path) or str(script_path),
                "-PayloadPath",
                _to_windows_path(payload_path) or str(payload_path),
            ]
            try:
                result = subprocess.run(command, capture_output=True, timeout=180)
            except subprocess.TimeoutExpired as exc:
                stdout = _decode_powershell_output(exc.stdout or b"")
                stderr = _decode_powershell_output(exc.stderr or b"")
                _write_export_log(log_path, stdout, stderr, timed_out=True)
                raise RuntimeError(
                    "La automatizacion de Visio excedio el tiempo limite de 180 segundos.\n"
                    "El proceso quedo esperando demasiado tiempo y se interrumpio.\n"
                    f"STDOUT parcial:\n{stdout}\nSTDERR parcial:\n{stderr}"
                ) from exc
            stdout = _decode_powershell_output(result.stdout)
            stderr = _decode_powershell_output(result.stderr)
            _write_export_log(log_path, stdout, stderr, timed_out=False)
            if result.returncode != 0:
                raise RuntimeError(
                    "Fallo la automatizacion de Visio.\n"
                    f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                )
            return stdout, stderr


def _decode_powershell_output(value: bytes) -> str:
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _write_export_log(log_path: Path, stdout: str, stderr: str, timed_out: bool) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(
                [
                    f"timed_out={str(timed_out).lower()}",
                    "",
                    "[stdout]",
                    stdout,
                    "",
                    "[stderr]",
                    stderr,
                ]
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def output_path_with_default(payload: dict) -> str:
    return str(payload.get("output_path", "export.vsdx")).replace(".vsdx", ".export.log.txt")


def _to_windows_path(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    if re.match(r"^[A-Za-z]:\\", text):
        return text
    if os.name != "posix":
        return text
    try:
        result = subprocess.run(
            ["wslpath", "-w", text],
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.strip() or text
    except Exception:
        return text


def _powershell_temp_root() -> Path:
    if os.name == "posix":
        cwd = Path.cwd()
        if str(cwd).startswith("/mnt/"):
            return cwd / ".pdf2bpmn_tmp"
        public_dir = Path("/mnt/c/Users/Public/.pdf_to_bpmn_visio_temp")
        return public_dir
    return Path(tempfile.gettempdir())


def _node_sort_key(node_type: NodeType) -> int:
    if node_type == NodeType.POOL:
        return 0
    if node_type == NodeType.LANE:
        return 1
    return 2


def _fallback_aliases_for(node_type: NodeType) -> list[str]:
    aliases: list[str] = []
    for fallback_type in MASTER_FALLBACKS.get(node_type, []):
        aliases.extend(MASTER_ALIASES.get(fallback_type, []))
    return aliases


def _infer_export_parent_map(diagram: DiagramDocument) -> dict[str, str | None]:
    active_nodes = [node for node in diagram.nodes if not node.deleted]
    pools = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.POOL]
    lanes = [node for node in active_nodes if _coerce_node_type(node.node_type) == NodeType.LANE]
    parent_map: dict[str, str | None] = {}

    for node in active_nodes:
        node_type = _coerce_node_type(node.node_type)
        explicit_parent = node.parent_id if diagram.find_node(node.parent_id or "") else None
        if explicit_parent:
            parent_map[node.id] = explicit_parent
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


def _smallest_container(node, containers):
    matches = [candidate for candidate in containers if candidate.id != node.id and _contains(candidate, node)]
    if not matches:
        return None
    matches.sort(key=lambda item: item.width * item.height)
    return matches[0]


def _contains(container, node) -> bool:
    margin = 2.0
    return (
        node.x >= container.x - margin
        and node.y >= container.y - margin
        and (node.x + node.width) <= (container.x + container.width + margin)
        and (node.y + node.height) <= (container.y + container.height + margin)
    )


def _edge_anchors(source, target) -> tuple[tuple[float, float], tuple[float, float]]:
    dx = target.center.x - source.center.x
    dy = target.center.y - source.center.y
    if abs(dx) >= abs(dy):
        if dx >= 0:
            return (1.0, 0.5), (0.0, 0.5)
        return (0.0, 0.5), (1.0, 0.5)
    if dy >= 0:
        return (0.5, 0.0), (0.5, 1.0)
    return (0.5, 1.0), (0.5, 0.0)


def _edge_anchors_from_bounds(source: dict, target: dict) -> tuple[tuple[float, float], tuple[float, float]]:
    source_center_x = float(source.get("x", 0.0)) + float(source.get("width", 0.0)) / 2.0
    source_center_y = float(source.get("y", 0.0)) + float(source.get("height", 0.0)) / 2.0
    target_center_x = float(target.get("x", 0.0)) + float(target.get("width", 0.0)) / 2.0
    target_center_y = float(target.get("y", 0.0)) + float(target.get("height", 0.0)) / 2.0
    dx = target_center_x - source_center_x
    dy = target_center_y - source_center_y
    if abs(dx) >= abs(dy):
        if dx >= 0:
            return (1.0, 0.5), (0.0, 0.5)
        return (0.0, 0.5), (1.0, 0.5)
    if dy >= 0:
        return (0.5, 0.0), (0.5, 1.0)
    return (0.5, 1.0), (0.5, 0.0)


def _inventory_script(settings: Settings) -> str:
    template_hint = settings.visio_template_hint or ""
    stencil_hint = settings.visio_stencil_hint or ""
    return f"""
param([string]$PayloadPath)
$ErrorActionPreference = "Stop"
$payload = Get-Content -Raw -Path $PayloadPath | ConvertFrom-Json

function Get-BpmnCandidates {{
  $results = New-Object System.Collections.Generic.List[string]
  $roots = @(
    "$env:ProgramFiles\\Microsoft Office",
    "$env:ProgramFiles (x86)\\Microsoft Office",
    "$env:ProgramFiles\\Microsoft Office\\root\\Office16",
    "$env:ProgramFiles (x86)\\Microsoft Office\\root\\Office16",
    "$env:ProgramFiles\\Microsoft Office\\root\\Office16\\Visio Content",
    "$env:ProgramFiles (x86)\\Microsoft Office\\root\\Office16\\Visio Content",
    "$env:LOCALAPPDATA\\Microsoft\\Visio"
  ) | Where-Object {{ $_ -and (Test-Path $_) }}

  if ("{template_hint}") {{
    if (Test-Path "{template_hint}") {{ $results.Add("{template_hint}") }}
  }}
  if ("{stencil_hint}") {{
    if (Test-Path "{stencil_hint}") {{ $results.Add("{stencil_hint}") }}
  }}

  foreach ($root in $roots) {{
    Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue |
      Where-Object {{ $_.Name -match "BPMN" -and $_.Extension -match "^\\.vs" }} |
      ForEach-Object {{ $results.Add($_.FullName) }}
  }}
  $results | Sort-Object -Unique
}}

$visio = New-Object -ComObject Visio.Application
$visio.Visible = $false
try {{
  $docs = @()
  $candidates = @(Get-BpmnCandidates)
  Write-Output ("Candidates: " + $candidates.Count)
  foreach ($candidate in $candidates) {{
    try {{
      $doc = $visio.Documents.OpenEx($candidate, 64)
      $docs += $doc
    }} catch {{
      Write-Output ("Open failed: " + $candidate)
    }}
  }}

  $inventory = @()
  foreach ($doc in $visio.Documents) {{
    $masters = @()
    foreach ($master in $doc.Masters) {{
      $masters += [PSCustomObject]@{{
        name = $master.Name
        nameU = $master.NameU
      }}
    }}
    $inventory += [PSCustomObject]@{{
      name = $doc.Name
      path = $doc.Path
      masters = $masters
    }}
  }}
  Write-Output ("Docs opened: " + $inventory.Count)
  if ($inventory.Count -eq 0) {{
    Set-Content -Path $payload.output_path -Encoding UTF8 -Value "[]"
  }} else {{
    ($inventory | ConvertTo-Json -Depth 20) | Set-Content -Path $payload.output_path -Encoding UTF8
  }}
}} finally {{
  $visio.Quit()
}}
"""


def _export_script(settings: Settings) -> str:
    template_hint = settings.visio_template_hint or ""
    stencil_hint = settings.visio_stencil_hint or ""
    return f"""
param([string]$PayloadPath)
$ErrorActionPreference = "Stop"
$payload = Get-Content -Raw -Path $PayloadPath | ConvertFrom-Json

function Resolve-TemplatePath {{
  if ($payload.template_hint -and (Test-Path $payload.template_hint)) {{ return $payload.template_hint }}
  if ("{template_hint}" -and (Test-Path "{template_hint}")) {{ return "{template_hint}" }}
  return $null
}}

function Resolve-StencilPath {{
  if ($payload.stencil_hint -and (Test-Path $payload.stencil_hint)) {{ return $payload.stencil_hint }}
  if ("{stencil_hint}" -and (Test-Path "{stencil_hint}")) {{ return "{stencil_hint}" }}
  return $null
}}

function Find-Master([string[]]$aliases) {{
  foreach ($doc in $visio.Documents) {{
    foreach ($master in $doc.Masters) {{
      foreach ($alias in $aliases) {{
        if ($master.Name -ieq $alias -or $master.NameU -ieq $alias) {{
          return $master
        }}
      }}
    }}
  }}
  foreach ($doc in $visio.Documents) {{
    foreach ($master in $doc.Masters) {{
      foreach ($alias in $aliases) {{
        if ($master.Name -like "*$alias*" -or $master.NameU -like "*$alias*") {{
          return $master
        }}
      }}
    }}
  }}
  return $null
}}

function Set-ShapeBounds($shape, [double]$pinX, [double]$pinY, [double]$width, [double]$height) {{
  foreach ($cellName in @("LockWidth", "LockHeight", "LockMoveX", "LockMoveY", "LockAspect", "ResizeAsNeeded")) {{
    try {{
      $shape.CellsU($cellName).FormulaU = "0"
    }} catch {{
    }}
  }}
  $shape.CellsU("Width").ResultIU = $width
  $shape.CellsU("Height").ResultIU = $height
  try {{
    $shape.CellsU("LocPinX").ResultIU = ($width / 2.0)
    $shape.CellsU("LocPinY").ResultIU = ($height / 2.0)
  }} catch {{
    Write-Output ("LocPin not adjustable for " + $shape.NameID)
  }}
  $shape.CellsU("PinX").ResultIU = $pinX
  $shape.CellsU("PinY").ResultIU = $pinY
}}

function Restore-ContainerBounds {{
  foreach ($node in ($payload.nodes | Sort-Object sort_key)) {{
    if ($node.deleted -or -not $node.is_container) {{ continue }}
    if (-not $shapeMap.ContainsKey($node.id)) {{ continue }}
    $shape = $shapeMap[$node.id]
    try {{
      Set-ShapeBounds $shape ([double]$node.pin_x) ([double]$node.pin_y) ([double]$node.width_in) ([double]$node.height_in)
    }} catch {{
      Write-Output ("Container bounds restore failed for " + $node.id)
      Write-Output $_.Exception.Message
      continue
    }}
    if ($node.type -eq "pool") {{
      try {{
        $shape.SendToBack()
      }} catch {{
        Write-Output ("SendToBack failed for " + $node.id)
        Write-Output $_.Exception.Message
      }}
    }}
  }}
}}

function Try-SetConnectorRoute($connector, $edge) {{
  try {{
    $connector.CellsU("ShapeRouteStyle").FormulaU = "16"
  }} catch {{
  }}
  try {{
    $connector.CellsU("ConLineRouteExt").FormulaU = "2"
  }} catch {{
  }}
  try {{
    $connector.CellsU("ObjType").FormulaU = "GUARD(2)"
  }} catch {{
  }}
  $waypoints = @($edge.waypoints)
  if ($waypoints.Count -lt 3) {{ return }}
  try {{
    $visSectionFirstComponent = 10
    $visRowVertex = 7
    $visTagLineTo = 3
    $visX = 0
    $visY = 1
    while ($connector.RowCount($visSectionFirstComponent) -gt 2) {{
      $connector.DeleteRow($visSectionFirstComponent, 1)
    }}
    for ($i = 1; $i -lt ($waypoints.Count - 1); $i++) {{
      $rowIndex = $connector.AddRow($visSectionFirstComponent, $i, $visRowVertex)
      $connector.RowType($visSectionFirstComponent, $rowIndex) = $visTagLineTo
      $connector.CellsSRC($visSectionFirstComponent, $rowIndex, $visX).ResultIU = [double]$waypoints[$i].x
      $connector.CellsSRC($visSectionFirstComponent, $rowIndex, $visY).ResultIU = [double]$waypoints[$i].y
    }}
  }} catch {{
    Write-Output ("Connector waypoint replay failed for " + $edge.id)
    Write-Output $_.Exception.Message
  }}
}}

$visio = New-Object -ComObject Visio.Application
$visio.Visible = $false
$visio.AlertResponse = 7
$document = $null
try {{
  $templatePath = Resolve-TemplatePath
  $stencilPath = Resolve-StencilPath
  Write-Output ("TemplatePath: " + [string]$templatePath)
  Write-Output ("StencilPath: " + [string]$stencilPath)

  if ($templatePath) {{
    Write-Output ("Opening template: " + $templatePath)
    $document = $visio.Documents.Add($templatePath)
  }} else {{
    Write-Output "Opening blank document"
    $document = $visio.Documents.Add("")
  }}

  if ($stencilPath) {{
    try {{
      Write-Output ("Opening stencil: " + $stencilPath)
      $visio.Documents.OpenEx($stencilPath, 64) | Out-Null
    }} catch {{
      Write-Output ("Stencil open failed: " + $stencilPath)
      Write-Output $_.Exception.Message
    }}
  }}

  $page = $visio.ActivePage
  $page.PageSheet.CellsU("PageWidth").ResultIU = [double]$payload.page_width_in
  $page.PageSheet.CellsU("PageHeight").ResultIU = [double]$payload.page_height_in

  $shapeMap = @{{}}
  foreach ($node in ($payload.nodes | Sort-Object sort_key)) {{
    if ($node.deleted) {{ continue }}
    $master = Find-Master $node.aliases
    if (-not $master -and $node.fallback_aliases) {{
      Write-Output ("Primary master not found for " + $node.type + ". Trying fallback aliases: " + ($node.fallback_aliases -join ', '))
      $master = Find-Master $node.fallback_aliases
    }}
    if (-not $master) {{
      throw "No se encontro master BPMN para aliases: $($node.aliases -join ', ')"
    }}
    $shape = $page.Drop($master, [double]$node.pin_x, [double]$node.pin_y)
    Set-ShapeBounds $shape ([double]$node.pin_x) ([double]$node.pin_y) ([double]$node.width_in) ([double]$node.height_in)
    if ($node.text) {{
      $shape.Text = [string]$node.text
    }}
    $shapeMap[$node.id] = $shape
  }}

  $visMemberAddDoNotExpand = 2
  foreach ($node in ($payload.nodes | Sort-Object sort_key)) {{
    if ($node.deleted) {{ continue }}
    if (-not $node.parent_id) {{ continue }}
    if (-not $shapeMap.ContainsKey($node.id) -or -not $shapeMap.ContainsKey($node.parent_id)) {{ continue }}
    $childShape = $shapeMap[$node.id]
    $parentShape = $shapeMap[$node.parent_id]
    try {{
      if ($node.type -ne "lane") {{
        Write-Output ("Attaching node " + $node.id + " to parent " + $node.parent_id)
        $parentShape.ContainerProperties.AddMember($childShape, $visMemberAddDoNotExpand)
      }} else {{
        Write-Output ("Skipping pool membership for lane " + $node.id + " to preserve explicit BPMN bounds")
      }}
    }} catch {{
      Write-Output ("Membership failed for " + $node.id + " -> " + $node.parent_id)
      Write-Output $_.Exception.Message
    }}
  }}
  Restore-ContainerBounds

  foreach ($edge in $payload.edges) {{
    if ($edge.deleted) {{ continue }}
    if (-not $shapeMap.ContainsKey($edge.source_id) -or -not $shapeMap.ContainsKey($edge.target_id)) {{
      continue
    }}
    $master = Find-Master $edge.aliases
    if (-not $master) {{
      throw "No se encontro master BPMN para aliases: $($edge.aliases -join ', ')"
    }}
    $connector = $page.Drop($master, 0, 0)
    $connector.CellsU("BeginX").GlueToPos($shapeMap[$edge.source_id], [double]$edge.source_anchor_x, [double]$edge.source_anchor_y)
    $connector.CellsU("EndX").GlueToPos($shapeMap[$edge.target_id], [double]$edge.target_anchor_x, [double]$edge.target_anchor_y)
    Try-SetConnectorRoute $connector $edge
    if ($edge.text) {{
      $connector.Text = [string]$edge.text
    }}
  }}
  Restore-ContainerBounds

  Write-Output ("Saving: " + [string]$payload.output_path)
  $document.SaveAs([string]$payload.output_path)
}} finally {{
  if ($document -and -not $payload.keep_visio_open) {{
    $document.Close()
  }}
  if (-not $payload.keep_visio_open) {{
    $visio.Quit()
  }}
}}
"""
