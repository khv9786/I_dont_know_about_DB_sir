from __future__ import annotations

from storage.models import TOKEN_PREFIX, Category, ProjectMapping


def next_token(category: Category, project: ProjectMapping) -> str:
    seq = project.next_sequence(category)
    return f"{TOKEN_PREFIX[category]}_{seq:03d}"


def get_or_create(
    project: ProjectMapping, category: Category, original: str, style: str | None = None
) -> str:
    token = project.find_token(category, original)
    if token is None:
        token = next_token(category, project)
        project.add(category, original, token, style=style)
    return token
