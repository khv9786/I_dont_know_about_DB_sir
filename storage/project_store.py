from __future__ import annotations

import json
import os
import re
from pathlib import Path

from storage.models import ProjectMapping

_INVALID_NAME_CHARS = re.compile(r"[^0-9A-Za-z가-힣_\-]+")


def _projects_dir() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / "QueryAnon" / "projects"
    base.mkdir(parents=True, exist_ok=True)
    return base


def sanitize_name(name: str) -> str:
    name = _INVALID_NAME_CHARS.sub("_", name.strip())
    return name or "default"


def list_projects() -> list[str]:
    return sorted(p.stem for p in _projects_dir().glob("*.json"))


def load(project_name: str) -> ProjectMapping:
    path = _projects_dir() / f"{sanitize_name(project_name)}.json"
    if not path.exists():
        return ProjectMapping(project_name=project_name)
    data = json.loads(path.read_text(encoding="utf-8"))
    return ProjectMapping.from_dict(data)


def save(mapping: ProjectMapping) -> None:
    path = _projects_dir() / f"{sanitize_name(mapping.project_name)}.json"
    path.write_text(json.dumps(mapping.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
