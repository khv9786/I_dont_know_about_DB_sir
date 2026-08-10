from __future__ import annotations

import re

from core.token_generator import get_or_create
from storage.models import ProjectMapping

Edit = tuple[int, int, str]  # (start, end_inclusive, replacement_text) — 원문 기준 절대 오프셋

_QUOTE_PAIRS = {'"': '"', "`": "`", "[": "]"}

_HEADER_PATTERN = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", re.IGNORECASE
)
_CONSTRAINT_LEAD_WORDS = {"constraint", "primary", "foreign", "unique", "check"}
_BARE_IDENT_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


class _Ident:
    __slots__ = ("name", "start", "end", "quote")

    def __init__(self, name: str, start: int, end: int, quote: str | None):
        self.name = name
        self.start = start
        self.end = end  # inclusive
        self.quote = quote  # None(따옴표 없음) | '"' | '`' | '['


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _extract_ident(text: str, pos: int) -> _Ident | None:
    """pos(공백 스킵 후)에서 시작하는 식별자 하나를 읽는다. 따옴표/백틱/대괄호 및 무따옴표 지원."""
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        return None
    ch = text[pos]
    if ch in _QUOTE_PAIRS:
        close = _QUOTE_PAIRS[ch]
        end = text.find(close, pos + 1)
        if end == -1:
            return None
        return _Ident(text[pos + 1 : end], pos, end, ch)
    match = _BARE_IDENT_PATTERN.match(text, pos)
    if not match:
        return None
    return _Ident(match.group(0), match.start(), match.end() - 1, None)


def _find_matching_paren(text: str, open_index: int) -> int | None:
    """open_index 위치의 '('에 대응하는 ')' 인덱스를 찾는다. 문자열 리터럴 내부는 건너뛴다."""
    depth = 0
    i, n = open_index, len(text)
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
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level_commas(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """[start, end) 구간을, 그 구간 기준 depth 0(중첩 괄호 밖)의 콤마로 나눈 (시작,끝) span 목록을 반환한다."""
    spans: list[tuple[int, int]] = []
    depth = 0
    item_start = start
    i = start
    while i < end:
        ch = text[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < end:
                if text[i] == quote:
                    if i + 1 < end and text[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((item_start, i))
            item_start = i + 1
        i += 1
    spans.append((item_start, end))
    return spans


def _ident_replacement(ident: _Ident, token: str) -> str:
    if ident.quote is None:
        return token
    close = _QUOTE_PAIRS[ident.quote]
    return f"{ident.quote}{token}{close}"


def _tokenize_paren_columns(
    text: str, open_paren: int, limit: int, project: ProjectMapping, edits: list[Edit]
) -> int | None:
    """open_paren 위치의 '(' ~ 대응 ')' 안 콤마구분 식별자들을 COLUMN으로 토큰화하고,
    그 ')' 다음 위치를 반환한다(범위를 못 찾거나 limit을 넘으면 None)."""
    close = _find_matching_paren(text, open_paren)
    if close is None or close > limit:
        return None
    for item_start, _item_end in _split_top_level_commas(text, open_paren + 1, close):
        ident = _extract_ident(text, item_start)
        if ident is None or not ident.name:
            continue
        token = get_or_create(project, "COLUMN", ident.name)
        edits.append((ident.start, ident.end, _ident_replacement(ident, token)))
    return close + 1


def _handle_constraint_item(
    text: str, item_start: int, item_end: int, project: ProjectMapping, edits: list[Edit]
) -> None:
    """PRIMARY KEY(...)/UNIQUE(...)/FOREIGN KEY(...) REFERENCES t(...) 형태의 테이블 레벨
    제약조건에서, 그 제약조건에 직접 딸린 괄호 그룹의 컬럼 참조만 골라 토큰화한다.

    이 뒤에 Oracle의 USING INDEX/STORAGE(...)/TABLESPACE 같은 저장 옵션이 콤마 없이
    바로 이어붙는 경우가 흔한데, 그 안의 STORAGE(...) 같은 무관한 괄호까지 스캔해
    잘못 토큰화하지 않도록 여기서 처리하는 괄호 그룹 개수를 명시적으로 제한한다.
    CHECK(...) 는 임의 표현식이라 안전하게 건드리지 않고 그대로 둔다(알려진 한계).
    """
    pos = _skip_ws(text, item_start)
    word = _WORD_PATTERN.match(text, pos)
    if not word:
        return
    lead = word.group(0).lower()
    pos = word.end()

    if lead == "constraint":
        name_ident = _extract_ident(text, pos)
        if name_ident is None:
            return
        pos = _skip_ws(text, name_ident.end + 1)
        word = _WORD_PATTERN.match(text, pos)
        if not word:
            return
        lead = word.group(0).lower()
        pos = word.end()

    if lead in ("primary", "unique"):
        pos = _skip_ws(text, pos)
        key_word = _WORD_PATTERN.match(text, pos)
        if lead == "primary" and key_word and key_word.group(0).lower() == "key":
            pos = key_word.end()
        pos = _skip_ws(text, pos)
        if pos < item_end and text[pos] == "(":
            _tokenize_paren_columns(text, pos, item_end, project, edits)
        return

    if lead == "foreign":
        pos = _skip_ws(text, pos)
        key_word = _WORD_PATTERN.match(text, pos)
        if key_word and key_word.group(0).lower() == "key":
            pos = key_word.end()
        pos = _skip_ws(text, pos)
        if pos >= item_end or text[pos] != "(":
            return
        after = _tokenize_paren_columns(text, pos, item_end, project, edits)
        if after is None:
            return
        pos = _skip_ws(text, after)
        ref_word = _WORD_PATTERN.match(text, pos)
        if not ref_word or ref_word.group(0).lower() != "references":
            return
        pos = _skip_ws(text, ref_word.end())
        table_ident = _extract_ident(text, pos)
        if table_ident is None or not table_ident.name:
            return
        token = get_or_create(project, "TABLE", table_ident.name)
        edits.append((table_ident.start, table_ident.end, _ident_replacement(table_ident, token)))
        pos = _skip_ws(text, table_ident.end + 1)
        if pos < item_end and text[pos] == "(":
            _tokenize_paren_columns(text, pos, item_end, project, edits)
        return


def try_anonymize_create_table(fragment: str, project: ProjectMapping) -> str | None:
    """CREATE TABLE 문 하나를 스키마.테이블명/컬럼명만 위치기반으로 토큰화한다.

    sqlglot이 파싱하지 못하는 Oracle STORAGE/PCTFREE/TABLESPACE/NOT NULL ENABLE 같은
    물리적 저장 옵션을 이해하려 들지 않고 원문 그대로 남긴다 — CREATE TABLE 헤더와
    컬럼 목록 괄호 구조만 직접 스캔하므로 이런 옵션이 무엇이든 깨지지 않는다.

    실패 시(구조를 못 찾으면) None을 반환해 호출부가 원래 sqlglot ParseError를
    그대로 올리게 한다. 이 함수 자체는 어떤 예외도 던지지 않는다(방어적으로 전부 잡음).
    """
    try:
        return _try_anonymize_create_table(fragment, project)
    except Exception:
        return None


def _try_anonymize_create_table(fragment: str, project: ProjectMapping) -> str | None:
    header = _HEADER_PATTERN.match(fragment.lstrip())
    if not header:
        return None
    pos = len(fragment) - len(fragment.lstrip()) + header.end()

    edits: list[Edit] = []

    # 스키마.테이블명 (부분마다 개별 TABLE 토큰 — 나머지 anonymizer와 동일한 관례)
    while True:
        ident = _extract_ident(fragment, pos)
        if ident is None or not ident.name:
            return None
        token = get_or_create(project, "TABLE", ident.name)
        edits.append((ident.start, ident.end, _ident_replacement(ident, token)))
        pos = _skip_ws(fragment, ident.end + 1)
        if pos < len(fragment) and fragment[pos] == ".":
            pos = _skip_ws(fragment, pos + 1)
            continue
        break

    if pos >= len(fragment) or fragment[pos] != "(":
        return None
    close = _find_matching_paren(fragment, pos)
    if close is None:
        return None

    for item_start, item_end in _split_top_level_commas(fragment, pos + 1, close):
        item_pos = _skip_ws(fragment, item_start)
        if item_pos >= item_end:
            continue
        word_match = _WORD_PATTERN.match(fragment, item_pos)
        is_constraint = bool(word_match) and word_match.group(0).lower() in _CONSTRAINT_LEAD_WORDS
        if is_constraint:
            _handle_constraint_item(fragment, item_pos, item_end, project, edits)
            continue
        ident = _extract_ident(fragment, item_pos)
        if ident is None or not ident.name:
            continue
        token = get_or_create(project, "COLUMN", ident.name)
        edits.append((ident.start, ident.end, _ident_replacement(ident, token)))

    result = fragment
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end + 1 :]
    return result
