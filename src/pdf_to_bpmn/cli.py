from __future__ import annotations

import argparse
from pathlib import Path

from pdf_to_bpmn.config import Settings
from pdf_to_bpmn.domain import DiagramDocument
from pdf_to_bpmn.services.analysis import HybridDiagramAnalyzer
from pdf_to_bpmn.services.rasterizer import SinglePagePdfRasterizer
from pdf_to_bpmn.services.storage import LocalWorkspaceStore
from pdf_to_bpmn.services.visio import VisioExporter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2bpmn",
        description="Reconstruye diagramas BPMN desde un PDF o imagen y exporta a Visio.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review", help="Analiza un PDF o imagen y abre la UI de revision.")
    review.add_argument(
        "input_path",
        nargs="?",
        type=Path,
        help="Ruta al archivo de entrada: PDF de una sola pagina o imagen compatible.",
    )
    review.add_argument("--output", type=Path, help="Ruta deseada para el .vsdx final.")

    inspect = subparsers.add_parser(
        "inspect-visio",
        help="Inspecciona templates y masters BPMN visibles para la instalacion de Visio.",
    )
    inspect.add_argument(
        "--output",
        type=Path,
        help="Archivo JSON de salida para el inventario.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "inspect-visio":
        exporter = VisioExporter(settings)
        destination = args.output or (settings.data_dir / "visio_inventory.json")
        exporter.inspect_installation(destination)
        print(f"Inventario Visio guardado en: {destination}")
        return 0

    if args.command == "review":
        store = LocalWorkspaceStore(settings)
        if args.input_path:
            input_path = args.input_path.expanduser().resolve()
            if not input_path.exists():
                parser.error(f"No existe el archivo de entrada: {input_path}")

            artifacts = store.create_run(input_path, args.output)

            rasterizer = SinglePagePdfRasterizer(settings.working_dpi)
            rasterizer.rasterize(artifacts.source_pdf, artifacts.source_image)

            analyzer = HybridDiagramAnalyzer(settings)
            diagram = analyzer.analyze(artifacts.source_pdf, artifacts.source_image)
            store.save_diagram(diagram, artifacts.diagram_json)
        else:
            artifacts = store.create_empty_run()
            _write_blank_png(artifacts.source_image)
            diagram = DiagramDocument(
                source_pdf=artifacts.source_pdf,
                source_image=artifacts.source_image,
                image_width=1600,
                image_height=900,
                metadata={"title": "Diagrama BPMN"},
            )
            store.save_diagram(diagram, artifacts.diagram_json)

        from pdf_to_bpmn.ui.main_window import launch_review_window

        exporter = VisioExporter(settings)
        return launch_review_window(settings, store, artifacts, diagram, exporter)

    parser.error("Comando no soportado.")
    return 2


def _write_blank_png(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV y numpy son necesarios para inicializar la imagen en blanco de la UI."
        ) from exc

    blank = np.full((900, 1600, 3), 255, dtype=np.uint8)
    if not cv2.imwrite(str(destination), blank):
        raise RuntimeError(f"No se pudo crear la imagen en blanco: {destination}")


if __name__ == "__main__":
    raise SystemExit(main())
