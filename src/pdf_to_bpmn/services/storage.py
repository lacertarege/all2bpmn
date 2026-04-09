from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import DiagramDocument


@dataclass(frozen=True)
class RunArtifacts:
    run_id: str
    run_dir: Path
    input_dir: Path
    output_dir: Path
    state_dir: Path
    source_pdf: Path
    source_image: Path
    diagram_json: Path
    visio_inventory_json: Path
    export_bpmn: Path
    export_bizagi_bpmn: Path
    export_xpdl: Path
    export_vsdx: Path


class LocalWorkspaceStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_run(self, input_path: Path, output_path: Path | None = None) -> RunArtifacts:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = self.settings.runs_dir / run_id
        input_dir = run_dir / "input"
        output_dir = run_dir / "output"
        state_dir = run_dir / "state"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        source_pdf = input_dir / input_path.name
        shutil.copy2(input_path, source_pdf)

        source_image = state_dir / "page.png"
        diagram_json = state_dir / "diagram.json"
        visio_inventory_json = state_dir / "visio_inventory.json"
        export_vsdx = (output_path or input_path.with_suffix(".vsdx")).resolve()
        export_bpmn = export_vsdx.with_suffix(".bpmn")
        export_bizagi_bpmn = export_vsdx.with_name(f"{export_vsdx.stem}.bizagi.bpmn")
        export_xpdl = export_vsdx.with_suffix(".xpdl")

        return RunArtifacts(
            run_id=run_id,
            run_dir=run_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            state_dir=state_dir,
            source_pdf=source_pdf,
            source_image=source_image,
            diagram_json=diagram_json,
            visio_inventory_json=visio_inventory_json,
            export_bpmn=export_bpmn,
            export_bizagi_bpmn=export_bizagi_bpmn,
            export_xpdl=export_xpdl,
            export_vsdx=export_vsdx,
        )

    def create_empty_run(self) -> RunArtifacts:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = self.settings.runs_dir / run_id
        input_dir = run_dir / "input"
        output_dir = run_dir / "output"
        state_dir = run_dir / "state"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        source_pdf = input_dir / "sin_entrada.dat"
        source_image = state_dir / "page.png"
        diagram_json = state_dir / "diagram.json"
        visio_inventory_json = state_dir / "visio_inventory.json"
        export_bpmn = output_dir / "sin_entrada.bpmn"
        export_bizagi_bpmn = output_dir / "sin_entrada.bizagi.bpmn"
        export_xpdl = output_dir / "sin_entrada.xpdl"
        export_vsdx = output_dir / "sin_entrada.vsdx"

        return RunArtifacts(
            run_id=run_id,
            run_dir=run_dir,
            input_dir=input_dir,
            output_dir=output_dir,
            state_dir=state_dir,
            source_pdf=source_pdf,
            source_image=source_image,
            diagram_json=diagram_json,
            visio_inventory_json=visio_inventory_json,
            export_bpmn=export_bpmn,
            export_bizagi_bpmn=export_bizagi_bpmn,
            export_xpdl=export_xpdl,
            export_vsdx=export_vsdx,
        )

    def save_diagram(self, diagram: DiagramDocument, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(diagram.to_json(), encoding="utf-8")

    def load_diagram(self, source: Path) -> DiagramDocument:
        return DiagramDocument.from_json(source.read_text(encoding="utf-8"))

    def archive_learning_sample(self, artifacts: RunArtifacts, diagram: DiagramDocument) -> Path:
        target_dir = self.settings.learning_dir / artifacts.run_id
        target_dir.mkdir(parents=True, exist_ok=True)

        for path in (
            artifacts.source_pdf,
            artifacts.source_image,
            artifacts.diagram_json,
        ):
            if path.exists():
                shutil.copy2(path, target_dir / path.name)

        metadata_path = target_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "run_id": artifacts.run_id,
                    "export_vsdx": str(artifacts.export_vsdx),
                    "issues_total": len(diagram.issues),
                    "issues_resolved": len([issue for issue in diagram.issues if issue.resolved]),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return target_dir
