from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _prepare_data_dir(raw_value: str) -> Path:
    expanded = _expand_path(raw_value)
    legacy = Path(raw_value).expanduser().resolve()
    if legacy != expanded and legacy.exists():
        expanded.mkdir(parents=True, exist_ok=True)
        for child in legacy.iterdir():
            target = expanded / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            elif not target.exists():
                shutil.copy2(child, target)
    return expanded


def _load_dotenv_files() -> None:
    cwd = Path.cwd()
    for candidate in (cwd / ".env", cwd / ".env.example"):
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    working_dpi: int
    confidence_threshold: float
    azure_doc_endpoint: str | None
    azure_doc_key: str | None
    azure_doc_model: str
    azure_doc_api_version: str
    azure_foundry_endpoint: str | None
    azure_foundry_responses_url: str | None
    azure_foundry_api_key: str | None
    azure_foundry_deployment: str | None
    azure_foundry_api_version: str
    visio_powershell: str
    visio_template_hint: str | None
    visio_stencil_hint: str | None
    export_keep_visio_open: bool
    open_bpmn_java: str
    open_bpmn_jar: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv_files()
        data_dir = _prepare_data_dir(
            os.getenv("PDF2BPMN_DATA_DIR", str(Path.home() / ".pdf_to_bpmn_visio"))
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "runs").mkdir(exist_ok=True)
        (data_dir / "learning").mkdir(exist_ok=True)
        return cls(
            data_dir=data_dir,
            working_dpi=int(os.getenv("PDF2BPMN_DPI", "300")),
            confidence_threshold=float(os.getenv("PDF2BPMN_CONFIDENCE", "0.75")),
            azure_doc_endpoint=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
            azure_doc_key=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY"),
            azure_doc_model=os.getenv(
                "AZURE_DOCUMENT_INTELLIGENCE_MODEL", "prebuilt-layout"
            ).strip(),
            azure_doc_api_version=os.getenv(
                "AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "2024-11-30"
            ),
            azure_foundry_endpoint=os.getenv("AZURE_FOUNDRY_ENDPOINT")
            or os.getenv("AZURE_OPENAI_ENDPOINT"),
            azure_foundry_responses_url=os.getenv("AZURE_FOUNDRY_RESPONSES_URL")
            or os.getenv("AZURE_FOUNDRY_CHAT_URL"),
            azure_foundry_api_key=os.getenv("AZURE_FOUNDRY_API_KEY")
            or os.getenv("AZURE_OPENAI_API_KEY"),
            azure_foundry_deployment=os.getenv("AZURE_FOUNDRY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT"),
            azure_foundry_api_version=os.getenv(
                "AZURE_FOUNDRY_API_VERSION",
                os.getenv("AZURE_OPENAI_API_VERSION", "v1"),
            ),
            visio_powershell=os.getenv("VISIO_POWERSHELL", "powershell.exe"),
            visio_template_hint=os.getenv("VISIO_BPMN_TEMPLATE_HINT"),
            visio_stencil_hint=os.getenv("VISIO_BPMN_STENCIL_HINT"),
            export_keep_visio_open=_as_bool(os.getenv("VISIO_KEEP_OPEN"), default=False),
            open_bpmn_java=os.getenv("OPEN_BPMN_JAVA", "java"),
            open_bpmn_jar=os.getenv("OPEN_BPMN_JAR"),
        )

    @property
    def has_document_intelligence(self) -> bool:
        return bool(self.azure_doc_endpoint and self.azure_doc_key)

    @property
    def has_foundry_vision(self) -> bool:
        if self.azure_foundry_responses_url and self.azure_foundry_api_key:
            return True
        return bool(
            self.azure_foundry_endpoint
            and self.azure_foundry_deployment
            and self.azure_foundry_api_key
        )

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def learning_dir(self) -> Path:
        return self.data_dir / "learning"
