"""Document IO: извлечение plain text из файла документа.

Тонкая обёртка над ``libs.office.extract_text`` с
поддержкой brief-режима для PDF (первые 100 стр. + до 300К символов
через pypdf).
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Корень платформы — от этого файла, а не от
#: ``cwd``: ``scripts/application/document_io.py`` → ``parents[5]`` = корень репозитория.
#: Парсер офисных файлов один на проект и живёт там
#: (change ``enterprise-mcp-platform``, фаза 11).
_PLATFORM_ROOT = Path(__file__).resolve().parents[5] / "mcp-platform"
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

from libs.office import extract_text


_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def load_text(path, *, mode: str = "full") -> str:
    """Извлечь plain text из файла через парсер (``libs.office``).

    ``mode='brief'`` для PDF: первые 100 стр. + до 300К символов через pypdf.
    ``mode='full'`` (по умолчанию): полная экстракция через pdfplumber/extract_text.
    """
    p = Path(path)
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат: '{p.suffix}'. "
            "legal_summarizer принимает только .pdf, .docx, .txt"
        )
    if mode == "brief" and p.suffix.lower() == ".pdf":
        text = _extract_pdf_head(p, max_pages=100, max_chars=300_000)
    else:
        text = extract_text(p)
    if not text or not text.strip():
        raise ValueError(
            f"Документ не содержит извлекаемого текста: {p}."
        )
    return text


def _extract_pdf_head(path: Path, *, max_pages: int, max_chars: int) -> str:
    """Извлечь первые ``max_pages`` страниц PDF через pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    parts: list[str] = []
    total = 0
    for i in range(min(max_pages, len(reader.pages))):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


__all__ = ["load_text"]
