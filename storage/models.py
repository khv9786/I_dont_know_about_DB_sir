from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["TABLE", "COLUMN", "ALIAS", "COMMENT"]

TOKEN_PREFIX: dict[Category, str] = {
    "TABLE": "TBL",
    "COLUMN": "COL",
    "ALIAS": "ALIAS",
    "COMMENT": "CMT",
}


@dataclass
class MappingEntry:
    original: str
    token: str
    category: Category
    style: str | None = None  # COMMENT 전용: "xml"(<!-- -->) | "sql"(/* */, --)


class ProjectMapping:
    """프로젝트 하나의 원본<->토큰 매핑 전체를 담는 런타임 객체.

    entries는 직렬화 대상이며, by_original/counters는 entries로부터
    로드 시점에 재구성되는 파생 인덱스다 (JSON에 중복 저장하지 않음).
    """

    def __init__(self, project_name: str, entries: list[MappingEntry] | None = None):
        self.project_name = project_name
        self.entries: dict[str, MappingEntry] = {}
        self.by_original: dict[tuple[Category, str], str] = {}
        self.counters: dict[Category, int] = {}
        for entry in entries or []:
            self._index(entry)

    def _index(self, entry: MappingEntry) -> None:
        self.entries[entry.token] = entry
        self.by_original[(entry.category, entry.original)] = entry.token
        suffix = entry.token.rsplit("_", 1)[-1]
        if suffix.isdigit():
            self.counters[entry.category] = max(self.counters.get(entry.category, 0), int(suffix))

    def find_token(self, category: Category, original: str) -> str | None:
        return self.by_original.get((category, original))

    def find_original(self, token: str) -> str | None:
        entry = self.entries.get(token)
        return entry.original if entry else None

    def add(self, category: Category, original: str, token: str, style: str | None = None) -> None:
        self._index(MappingEntry(original=original, token=token, category=category, style=style))

    def next_sequence(self, category: Category) -> int:
        n = self.counters.get(category, 0) + 1
        self.counters[category] = n
        return n

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "entries": [
                {"token": e.token, "original": e.original, "category": e.category, "style": e.style}
                for e in self.entries.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectMapping":
        entries = [
            MappingEntry(
                original=e["original"], token=e["token"], category=e["category"], style=e.get("style")
            )
            for e in data.get("entries", [])
        ]
        return cls(project_name=data["project_name"], entries=entries)
