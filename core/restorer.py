from __future__ import annotations

import re

from storage.models import ProjectMapping

_TOKEN_PATTERN = re.compile(r"\b(?:TBL|COL|ALIAS|CMT|VAL)_\d+\b", re.IGNORECASE)


def _restore_xml_comments(text: str, project: ProjectMapping) -> str:
    """style="xml"로 기록된 CMT 토큰은 /* TOKEN */ 형태를 통째로 <!--원본--> 으로 되돌린다.

    이 패스가 먼저 소비한 토큰은 뒤따르는 범용 토큰 치환 대상에서 자연히 빠진다.
    """
    for entry in project.entries.values():
        if entry.category != "COMMENT" or entry.style != "xml":
            continue
        pattern = re.compile(r"/\*\s*" + re.escape(entry.token) + r"\s*\*/", re.IGNORECASE)
        text = pattern.sub(lambda _m, o=entry.original: f"<!--{o}-->", text)
    return text


def restore(text: str, project: ProjectMapping) -> str:
    """LLM 응답(SQL이 아닐 수도 있는 자유 텍스트)에서 토큰을 원본으로 역치환한다.

    LLM이 토큰 대소문자를 바꿔 쓸 수 있어 대소문자 무시로 매칭한 뒤
    저장된 표준 형식(대문자)으로 정규화해 조회한다.
    """
    text = _restore_xml_comments(text, project)

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0).upper()
        original = project.find_original(token)
        return original if original is not None else match.group(0)

    return _TOKEN_PATTERN.sub(_replace, text)
