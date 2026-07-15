from __future__ import annotations

from sqlglot import exp

from core import mapper_xml, parser
from core.token_generator import get_or_create as _get_or_create
from storage.models import Category, ProjectMapping

Edit = tuple[int, int, str]  # (start, end_inclusive, replacement_text) — 원문 기준 절대 오프셋


def _add_identifier_edit(
    edits: list[Edit],
    project: ProjectMapping,
    ident: exp.Expression | None,
    category: Category,
    token: str | None = None,
) -> None:
    if ident is None or not ident.name:
        return
    start, end = ident.meta.get("start"), ident.meta.get("end")
    if start is None or end is None:
        return
    edits.append((start, end, token if token is not None else _get_or_create(project, category, ident.name)))


def _collect_renames(ast: exp.Expression, project: ProjectMapping) -> list[Edit]:
    """AST는 건드리지 않고, (원문 시작, 원문 끝, 치환할 토큰) 목록만 모은다."""
    edits: list[Edit] = []

    for table in parser.iter_tables(ast):
        _add_identifier_edit(edits, project, table.args.get("this"), "TABLE")
        _add_identifier_edit(edits, project, table.args.get("db"), "TABLE")
        _add_identifier_edit(edits, project, table.args.get("catalog"), "TABLE")

    for table_alias in parser.iter_table_aliases(ast):
        _add_identifier_edit(edits, project, table_alias.args.get("this"), "ALIAS")

    for column in parser.iter_columns(ast):
        _add_identifier_edit(edits, project, column.args.get("this"), "COLUMN")

        qualifier_ident = column.args.get("table")
        if qualifier_ident is not None and qualifier_ident.name:
            qualifier = qualifier_ident.name
            token = project.find_token("TABLE", qualifier) or project.find_token("ALIAS", qualifier)
            if token is None:
                token = _get_or_create(project, "ALIAS", qualifier)
            _add_identifier_edit(edits, project, qualifier_ident, "ALIAS", token=token)

    for alias_node in parser.iter_column_aliases(ast):
        _add_identifier_edit(edits, project, alias_node.args.get("alias"), "ALIAS")

    return edits


def _splice(text: str, edits: list[Edit]) -> str:
    """뒤에서부터 앞으로 잘라 붙여서, 앞쪽 offset이 무효화되지 않게 한다."""
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end + 1 :]
    return text


def anonymize(sql: str, project: ProjectMapping, dialect: str | None = None) -> tuple[str, int]:
    """반환값: (익명화된 SQL, 감지된 동적 태그 개수).

    sqlglot의 AST/재생성(ast.sql())은 쓰지 않는다 — 식별자가 원문 몇 번째 문자에
    있는지 찾는 용도로만 sqlglot을 쓰고, 그 위치를 원문에서 직접 잘라 바꿔치기한다.
    그래서 원본 서식·주석 위치·동적 태그 위치가 전부 그대로 유지된다.
    """
    masked = mapper_xml.mask_all_comments(sql, project)
    prefix, inner, suffix = mapper_xml.strip_outer_tag(masked)
    inner = mapper_xml.unwrap_cdata(inner)
    dynamic_tag_count = mapper_xml.count_dynamic_tags(inner)
    inner = mapper_xml.mask_dynamic_tags(inner, project)
    inner, bind_specs = mapper_xml.protect_bind_vars(inner, project)

    statements = parser.parse_all(inner, dialect=dialect)
    edits: list[Edit] = []
    for ast in statements:
        edits.extend(_collect_renames(ast, project))
    result = _splice(inner, edits)

    result = mapper_xml.restore_bind_var_placeholders(result, bind_specs)
    result = mapper_xml.unmask_dynamic_tags(result)
    return f"{prefix}{result}{suffix}", dynamic_tag_count
