from __future__ import annotations

import re
from dataclasses import dataclass

from core.token_generator import get_or_create
from storage.models import ProjectMapping

_OUTER_TAG_PATTERN = re.compile(
    r"(<([a-zA-Z][\w:.-]*)(?:\s[^<>]*)?>)(.*)(</\2>)\s*$",
    re.DOTALL,
)

_COMMENT_PATTERN = re.compile(
    r"<!--(?P<xml>.*?)-->" r"|/\*(?P<block>.*?)\*/" r"|--(?P<line>[^\n]*)",
    re.DOTALL,
)

_CDATA_PATTERN = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)

_BIND_VAR_PATTERN = re.compile(r"#\{(\w+)\}|#(\w+)#|\$\{(\w+)\}")
_TEMP_PLACEHOLDER = "__QA_BINDVAR_{index}__"
_BINDVAR_PLACEHOLDER_PATTERN = re.compile(r"^__QA_BINDVAR_\d+__$")


def is_bindvar_placeholder(value: str) -> bool:
    """protect_bind_vars가 심어둔 임시 문자열 리터럴인지 판별한다.

    단순 SQL 모드의 리터럴 익명화가 이 임시 플레이스홀더까지 VALUE 토큰으로
    덮어써버리면 restore_bind_var_placeholders가 원래 바인드 변수명을 찾지
    못하게 되므로, 리터럴 순회 단계에서 이 값들을 걸러내는 용도로 쓴다.
    """
    return bool(_BINDVAR_PLACEHOLDER_PATTERN.fullmatch(value))


def strip_outer_tag(text: str) -> tuple[str, str, str]:
    """<select ...>...</select> 형태면 (여는태그, 내부내용, 닫는태그)를 반환한다.

    태그 앞에 whitespace나 (이미 마스킹된) 리딩 주석이 있어도 prefix로 흡수한다.
    text 전체가 단일 요소일 때 쓴다 — 여러 요소가 섞인 파일은 find_query_elements로
    먼저 요소 단위로 잘라낸 뒤 각 조각에 대해 이 함수를 호출한다.
    """
    match = _OUTER_TAG_PATTERN.search(text)
    if not match:
        return "", text, ""
    prefix = text[: match.start()]
    return prefix + match.group(1), match.group(3), match.group(4)


_STATEMENT_TAG_NAMES = ("select", "insert", "update", "delete", "sql")
_STATEMENT_OPEN_PATTERN = re.compile(
    r"<(" + "|".join(_STATEMENT_TAG_NAMES) + r")\b(?:\s[^<>]*)?>",
    re.IGNORECASE,
)


def find_query_elements(text: str) -> list[tuple[int, int]]:
    """<select>/<insert>/<update>/<delete>/<sql> 최상위 요소들의 (시작, 끝) 오프셋 목록을 찾는다.

    이 태그들끼리는 서로 중첩되지 않는다는 iBatis/MyBatis 매퍼 관례를 전제로,
    각 여는 태그 뒤에서 처음 만나는 같은 이름의 닫는 태그를 그 요소의 끝으로 본다.
    <sqlMap>/<mapper> 같은 루트 래퍼나 요소 사이 공백은 여기서 다루지 않고
    호출부가 그대로 원문 유지한다.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        open_match = _STATEMENT_OPEN_PATTERN.search(text, pos)
        if not open_match:
            break
        tagname = open_match.group(1)
        close_pattern = re.compile(r"</\s*" + re.escape(tagname) + r"\s*>", re.IGNORECASE)
        close_match = close_pattern.search(text, open_match.end())
        if not close_match:
            break
        spans.append((open_match.start(), close_match.end()))
        pos = close_match.end()
    return spans


_STATEMENT_KEYWORD_PATTERN = re.compile(
    r"^[ \t]*(select|insert|update|delete|with|merge|replace|create|alter|drop)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PRECEDING_WORD_PATTERN = re.compile(r"([a-zA-Z_][a-zA-Z_0-9]*)\s*$")
_SET_COMBINATOR_WORDS = {"union", "intersect", "except", "minus", "all", "distinct"}


def _top_level_mask(text: str) -> list[bool]:
    """각 문자 위치가 괄호/문자열/주석 밖(최상위, depth 0)인지 표시한다.

    find_plain_statement_spans가 서브쿼리나 문자열 리터럴 안의 SELECT 등을
    문장 경계로 오인하지 않도록 하는 용도다.
    """
    mask = [False] * len(text)
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = end if end != -1 else n
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        else:
            mask[i] = depth == 0
        i += 1
    return mask


def find_plain_statement_spans(text: str) -> list[tuple[int, int]]:
    """세미콜론 없이(혹은 일부만) 줄바꿈으로만 이어붙인 여러 SQL 문장을 각각의
    (시작, 끝) 오프셋으로 나눈다.

    parser.parse_all이 세미콜론 부족으로 ParseError를 낼 때만 anonymizer가
    복구 경로로 호출한다 — 정상 파싱되는 입력(제대로 된 ;, 단일 문장)은 이 함수를
    거치지 않고 기존 경로 그대로 처리된다.

    최상위(depth 0)에서 줄이 SELECT/INSERT/UPDATE/DELETE/WITH/MERGE/REPLACE로
    시작하면 새 문장의 시작으로 본다. 단 그 직전 최상위 단어가 UNION/INTERSECT/
    EXCEPT/MINUS/ALL/DISTINCT면 집합 연산으로 이어지는 같은 문장이므로 분리하지 않는다.
    문장 경계를 2개 미만으로 찾으면(=이 휴리스틱으로도 답이 안 나오면) 빈 리스트를
    반환해 호출부가 원래 ParseError를 그대로 올리게 한다.
    """
    top_level = _top_level_mask(text)
    candidates: list[int] = []
    for match in _STATEMENT_KEYWORD_PATTERN.finditer(text):
        kw_start = match.start(1)
        if not top_level[kw_start]:
            continue
        preceding = text[: match.start()].rstrip()
        word_match = _PRECEDING_WORD_PATTERN.search(preceding)
        if word_match and word_match.group(1).lower() in _SET_COMBINATOR_WORDS:
            continue
        candidates.append(kw_start)

    if len(candidates) < 2:
        return []

    spans: list[tuple[int, int]] = []
    for idx, start in enumerate(candidates):
        end = candidates[idx + 1] if idx + 1 < len(candidates) else len(text)
        spans.append((start, end))
    return spans


def unwrap_cdata(text: str) -> str:
    """<![CDATA[ >= ]]> 는 보통 '<'/'>' 를 포함한 SQL 비교연산자를 XML 이스케이프한 것이다.

    내부 내용(연산자) 자체는 민감정보가 아니고 그대로도 유효한 SQL이므로
    래퍼만 벗겨서 파싱을 통과시킨다. 복원 시 CDATA 래퍼는 다시 씌우지 않고
    평범한 SQL 연산자(예: >=)로 남는다 — 이 SQL을 다시 XML 매퍼 파일에
    그대로 붙여넣을 계획이라면 '<', '<=' 등은 수동으로 다시 CDATA나
    &lt; 로 escape 해줘야 한다.
    """
    return _CDATA_PATTERN.sub(lambda m: m.group(1), text)


def mask_all_comments(text: str, project: ProjectMapping) -> str:
    """<!-- -->, /* */, -- 세 종류의 주석을 한 번에 찾아 토큰화한다.

    splice 방식에서는 sqlglot AST의 주석을 전혀 읽지 않으므로(식별자 위치만 사용),
    주석 마스킹은 파싱 전에 이 함수 하나로 전부 끝내야 한다. 세 패턴을 한 정규식의
    대안(|)으로 묶어 겹쳐 매칭될 여지를 구조적으로 없앤다(이중 마스킹 불가).
    XML 주석은 SQL이 파싱 가능하도록 /* */ 형태로 바뀌고, 나머지는 원래 구분자를 유지한다.
    """

    def _replace(match: re.Match[str]) -> str:
        if match.group("xml") is not None:
            token = get_or_create(project, "COMMENT", match.group("xml"), style="xml")
            return f"/* {token} */"
        if match.group("block") is not None:
            token = get_or_create(project, "COMMENT", match.group("block"), style="sql")
            return f"/*{token}*/"
        token = get_or_create(project, "COMMENT", match.group("line"), style="sql")
        return f"--{token}"

    return _COMMENT_PATTERN.sub(_replace, text)


_DYNAMIC_TAG_PATTERN = re.compile(
    r"<(/?)([a-zA-Z][\w:.-]*)((?:\s+[a-zA-Z_:][\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)\s*>"
)
_ATTR_PATTERN = re.compile(r"([a-zA-Z_:][\w:.-]*)\s*=\s*(\"[^\"]*\"|'[^']*')")
_MARKER_PATTERN = re.compile(r"/\*\s*@QA_(OPEN|CLOSE|TAG):([a-zA-Z][\w:.-]*)((?:\s+[^@]*?)?)\s*@\s*\*/")


def count_dynamic_tags(text: str) -> int:
    """복원 시 위치가 어긋날 수 있는 동적 태그 개수를 센다 (GUI 경고용).

    strip_outer_tag로 최상위 <select> 등을 걷어낸 '내부' 텍스트에 대해 호출해야
    최상위 태그 자체가 오탐되지 않는다.
    """
    return len(_DYNAMIC_TAG_PATTERN.findall(text))


def mask_dynamic_tags(text: str, project: ProjectMapping) -> str:
    """<isNotEmpty property="x">...</isNotEmpty>, <include refid="x"/> 처럼 남아있는
    (동적 SQL) XML 태그를 SQL 블록주석 마커로 바꿔 sqlglot 파싱을 통과시킨다.

    splice 방식에서는 이 마커 텍스트가 최종 결과문자열 안에 원래 위치 그대로
    남아있으므로(식별자 span만 잘라 치환하고 나머지는 원문 그대로 유지) unmask_dynamic_tags가
    정확한 위치에서 이를 되돌릴 수 있다. property/refid 같은 속성값은 바인드변수와
    동일한 COLUMN 카테고리로 마스킹해 #x# 형태로 이미 나온 토큰과 일치시킨다.
    """

    def _mask_attrs(attrs_str: str) -> str:
        def _attr_repl(m: re.Match[str]) -> str:
            name, quoted_val = m.group(1), m.group(2)
            quote = quoted_val[0]
            value = quoted_val[1:-1]
            token = get_or_create(project, "COLUMN", value)
            return f"{name}={quote}{token}{quote}"

        return _ATTR_PATTERN.sub(_attr_repl, attrs_str)

    def _replace(match: re.Match[str]) -> str:
        is_close, tagname, attrs, self_close = match.groups()
        masked_attrs = _mask_attrs(attrs)
        if is_close:
            return f"/*@QA_CLOSE:{tagname}@*/"
        if self_close:
            return f"/*@QA_TAG:{tagname}{masked_attrs}@*/"
        return f"/*@QA_OPEN:{tagname}{masked_attrs}@*/"

    return _DYNAMIC_TAG_PATTERN.sub(_replace, text)


def unmask_dynamic_tags(text: str) -> str:
    """mask_dynamic_tags가 심어둔 마커 주석을 실제 XML 태그로 되돌린다."""

    def _replace(match: re.Match[str]) -> str:
        kind, tagname, attrs = match.groups()
        attrs_part = f" {attrs.strip()}" if attrs and attrs.strip() else ""
        if kind == "CLOSE":
            return f"</{tagname}>"
        if kind == "TAG":
            return f"<{tagname}{attrs_part}/>"
        return f"<{tagname}{attrs_part}>"

    return _MARKER_PATTERN.sub(_replace, text)


@dataclass
class BindVarSpec:
    index: int
    token: str
    style: str  # "hash_brace"(#{x}) | "hash"(#x#) | "dollar_brace"(${x})


def protect_bind_vars(text: str, project: ProjectMapping) -> tuple[str, list[BindVarSpec]]:
    """#schSpotNm# / #{schSpotNm} / ${schSpotNm} 를 COLUMN 카테고리로 토큰화하고,
    sqlglot가 안전하게 파싱하도록 임시 문자열 리터럴로 치환한다.
    """
    specs: list[BindVarSpec] = []

    def _replace(match: re.Match[str]) -> str:
        index = len(specs)
        if match.group(1) is not None:
            name, style = match.group(1), "hash_brace"
        elif match.group(2) is not None:
            name, style = match.group(2), "hash"
        else:
            name, style = match.group(3), "dollar_brace"
        token = get_or_create(project, "COLUMN", name)
        specs.append(BindVarSpec(index=index, token=token, style=style))
        return f"'{_TEMP_PLACEHOLDER.format(index=index)}'"

    protected = _BIND_VAR_PATTERN.sub(_replace, text)
    return protected, specs


def restore_bind_var_placeholders(sql: str, specs: list[BindVarSpec]) -> str:
    """sqlglot 재생성 후, 임시 문자열 리터럴을 다시 원래 구분자 스타일의 토큰으로 되돌린다."""
    result = sql
    for spec in specs:
        placeholder = _TEMP_PLACEHOLDER.format(index=spec.index)
        if spec.style == "hash_brace":
            replacement = f"#{{{spec.token}}}"
        elif spec.style == "dollar_brace":
            replacement = f"${{{spec.token}}}"
        else:
            replacement = f"#{spec.token}#"
        result = re.sub(r"""['"]""" + re.escape(placeholder) + r"""['"]""", replacement, result)
    return result
