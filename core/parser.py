from __future__ import annotations

from typing import Iterator

import sqlglot
from sqlglot import exp


def parse_all(sql: str, dialect: str | None = None) -> list[exp.Expression]:
    """세미콜론으로 구분된 여러 statement를 모두 파싱해서 반환한다."""
    statements = sqlglot.parse(sql, read=dialect)
    return [s for s in statements if s is not None]


def iter_tables(ast: exp.Expression) -> Iterator[exp.Table]:
    yield from ast.find_all(exp.Table)


def iter_columns(ast: exp.Expression) -> Iterator[exp.Column]:
    yield from ast.find_all(exp.Column)


def iter_table_aliases(ast: exp.Expression) -> Iterator[exp.TableAlias]:
    yield from ast.find_all(exp.TableAlias)


def iter_column_aliases(ast: exp.Expression) -> Iterator[exp.Alias]:
    yield from ast.find_all(exp.Alias)


def iter_schema_column_identifiers(ast: exp.Expression) -> Iterator[exp.Identifier]:
    """INSERT INTO t (a, b) VALUES (...) 의 (a, b)처럼 Column이 아니라
    Schema.expressions에 바로 담기는 컬럼 식별자들을 찾는다."""
    for schema in ast.find_all(exp.Schema):
        for e in schema.expressions:
            if isinstance(e, exp.Identifier):
                yield e
