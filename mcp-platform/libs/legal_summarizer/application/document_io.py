"""Document IO: извлечение plain text из файла документа.

Тонкая обёртка над ``libs.office.extract_text``. Один путь на все форматы:
режима извлечения не существует.
"""

from __future__ import annotations

from pathlib import Path

from libs.office import extract_text

_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def load_text(path) -> str:
    """Извлечь plain text из файла через парсер (``libs.office``).

    Извлечение **одно** для обоих режимов свода, и это не «упрощение», а
    требование: структура документа строится без LLM, поэтому строить её
    по-разному в зависимости от режима означало бы две разные структуры
    одного и того же файла.

    Раньше здесь был режим ``brief`` — первые 100 страниц PDF плюс 300 000
    символов через pypdf, — и он лгал молча: на документе длиннее 100
    страниц структура строилась по усечённому тексту, то есть outline
    описывал только начало, и модель отвечала про документ, который видела
    наполовину. Объём входа ограничивает не извлечение, а сборка brief-чанка
    (``brief_context.BriefContextConfig``: ``max_chars`` от окна модели плюс
    ``structure_max_chars`` на outline).
    """
    p = Path(path)
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат: '{p.suffix}'. "
            "legal_summarizer принимает только .pdf, .docx, .txt"
        )
    text = extract_text(p)
    if not text or not text.strip():
        raise ValueError(
            f"Документ не содержит извлекаемого текста: {p}."
        )
    return text


__all__ = ["load_text"]