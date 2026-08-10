from __future__ import annotations

from sqlglot import exp
from sqlglot.errors import ParseError

from core import ddl_parser, mapper_xml, parser
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


def _add_literal_edit(edits: list[Edit], project: ProjectMapping, literal: exp.Literal) -> None:
    """문자열 리터럴 하나를 VALUE 카테고리로 토큰화한다.

    literal.meta의 start/end는 따옴표까지 포함한 span이므로(sqlglot 30.12.0 확인됨),
    치환문에도 작은따옴표를 직접 씌워 유효한 SQL 문자열 리터럴 형태를 유지한다.
    """
    if not literal.name or mapper_xml.is_bindvar_placeholder(literal.name):
        return
    start, end = literal.meta.get("start"), literal.meta.get("end")
    if start is None or end is None:
        return
    token = _get_or_create(project, "VALUE", literal.name)
    edits.append((start, end, f"'{token}'"))


def _collect_renames(
    ast: exp.Expression, project: ProjectMapping, anonymize_literals: bool = False
) -> list[Edit]:
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

    for column_def in parser.iter_column_defs(ast):
        _add_identifier_edit(edits, project, column_def.args.get("this"), "COLUMN")

    for alias_node in parser.iter_column_aliases(ast):
        _add_identifier_edit(edits, project, alias_node.args.get("alias"), "ALIAS")

    for schema_ident in parser.iter_schema_column_identifiers(ast):
        _add_identifier_edit(edits, project, schema_ident, "COLUMN")

    if anonymize_literals:
        for literal in ast.find_all(exp.Literal):
            if literal.is_string:
                _add_literal_edit(edits, project, literal)

    return edits


def _splice(text: str, edits: list[Edit]) -> str:
    """뒤에서부터 앞으로 잘라 붙여서, 앞쪽 offset이 무효화되지 않게 한다."""
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end + 1 :]
    return text


def _anonymize_fragment(
    masked: str, project: ProjectMapping, dialect: str | None, anonymize_literals: bool = False
) -> tuple[str, int]:
    """주석 마스킹이 이미 끝난 조각(단일 <select> 요소 또는 순수 SQL) 하나를 처리한다."""
    prefix, inner, suffix = mapper_xml.strip_outer_tag(masked)
    inner = mapper_xml.unwrap_cdata(inner)
    dynamic_tag_count = mapper_xml.count_dynamic_tags(inner)
    inner = mapper_xml.mask_dynamic_tags(inner, project)
    inner, bind_specs = mapper_xml.protect_bind_vars(inner, project)

    statements = parser.parse_all(inner, dialect=dialect)
    edits: list[Edit] = []
    for ast in statements:
        edits.extend(_collect_renames(ast, project, anonymize_literals=anonymize_literals))
    result = _splice(inner, edits)

    result = mapper_xml.restore_bind_var_placeholders(result, bind_specs)
    result = mapper_xml.unmask_dynamic_tags(result)
    return f"{prefix}{result}{suffix}", dynamic_tag_count


def anonymize(
    sql: str,
    project: ProjectMapping,
    dialect: str | None = None,
    anonymize_literals: bool = False,
) -> tuple[str, int]:
    """반환값: (익명화된 SQL, 감지된 동적 태그 개수).

    sqlglot의 AST/재생성(ast.sql())은 쓰지 않는다 — 식별자가 원문 몇 번째 문자에
    있는지 찾는 용도로만 sqlglot을 쓰고, 그 위치를 원문에서 직접 잘라 바꿔치기한다.
    그래서 원본 서식·주석 위치·동적 태그 위치가 전부 그대로 유지된다.

    <select>/<insert>/<update>/<delete>/<sql> 요소가 여러 개 있는 XML 파일 전체를
    붙여넣어도, 각 요소를 찾아 개별 처리한 뒤 원래 순서/사이 공백 그대로 재조립한다.
    프로젝트 매핑은 파일 전체에서 공유되므로 여러 쿼리에 걸쳐 같은 이름은 같은 토큰이 된다.

    anonymize_literals=True("단순 SQL" 모드)면 문자열 리터럴(예: '홍길동')도 VALUE
    카테고리로 토큰화한다. MyBatis 매퍼는 실제 값이 보통 바인드 변수(#{})로 빠져있어
    기본값(False)을 쓰며, 켤 경우 'Y'/'N' 같은 상태 플래그 리터럴까지 토큰화되어
    매퍼 가독성이 떨어질 수 있다.
    """
    masked = mapper_xml.mask_all_comments(sql, project)
    spans = mapper_xml.find_query_elements(masked)

    if not spans:
        try:
            return _anonymize_fragment(masked, project, dialect, anonymize_literals=anonymize_literals)
        except ParseError:
            # 세미콜론 없이 줄바꿈으로만 이어붙인 여러 SQL 문장일 수 있다 — 문장 경계를
            # 찾아 복구를 시도하고, 그마저 안 되면 원래 에러를 그대로 올린다.
            spans = mapper_xml.find_plain_statement_spans(masked)
            if not spans:
                # Oracle STORAGE/PCTFREE/TABLESPACE 등 sqlglot이 이해 못하는 DDL
                # 저장 옵션 때문일 수 있다 — CREATE TABLE 전용 경량 추출기로 재시도.
                ddl_result = ddl_parser.try_anonymize_create_table(masked, project)
                if ddl_result is not None:
                    return ddl_result, 0
                raise

    pieces: list[str] = []
    last_end = 0
    total_dynamic_tag_count = 0
    for start, end in spans:
        pieces.append(masked[last_end:start])
        processed, dyn = _anonymize_fragment(
            masked[start:end], project, dialect, anonymize_literals=anonymize_literals
        )
        pieces.append(processed)
        total_dynamic_tag_count += dyn
        last_end = end
    pieces.append(masked[last_end:])
    return "".join(pieces), total_dynamic_tag_count
