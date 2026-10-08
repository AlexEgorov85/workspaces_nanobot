"""Plain-text рендер финального отчёта (без markdown-разметки).

Снимает ``##``/``###``, ``**bold**``, ``*italic*``, ``` `code` ```,
``> blockquote`` и ``---``. Использует ``markdown.render_markdown`` как
промежуточный шаг.
"""

from __future__ import annotations

import re
from typing import Any

from workspace.skills.audit_formulation_strengthener.scripts.report.markdown import (
    render_markdown,
)


__all__ = ["render_plain"]


_HEADER_RE = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BLOCKQUOTE_RE = re.compile(r"^>\s?", re.MULTILINE)
_HR_RE = re.compile(r"^---+\s*$", re.MULTILINE)


def render_plain(data: dict[str, Any]) -> str:
    """Собрать plain-text отчёт (без markdown-разметки)."""
    md = render_markdown(data)
    out = md
    out = _BOLD_RE.sub(r"\1", out)
    out = _ITALIC_RE.sub(r"\1", out)
    out = _INLINE_CODE_RE.sub(r"\1", out)
    out = _BLOCKQUOTE_RE.sub("", out)
    out = _HEADER_RE.sub("", out)
    out = _HR_RE.sub("", out)
    # Сжать 3+ пустых строк до 2.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"
