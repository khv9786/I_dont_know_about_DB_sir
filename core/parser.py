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
