# -*- coding: utf-8 -*-
"""
html_presentation_generator — навык nanobot для генерации HTML-презентаций.

Генерирует самодостаточные HTML-файлы из Markdown с:
    - Разделением на слайды (---)
    - Mermaid-диаграммами (```mermaid ... ```)
    - Таблицами, списками, форматированием
    - Клавиатурной навигацией (← → Space F Home End)
    - Прогресс-баром и кнопками prev/next/fullscreen
    - Адаптивным дизайном и поддержкой печати


Быстрый старт
-------------

Через CLI:
    generate_presentation --input examples/input.md --output out.html --title "Мой доклад"

Через агента (tool):
    generate_presentation(
        input="# Доклад\\n\\n---\\n\\n## Слайд 2\\nТекст",
        output="report.html",
        title="Анализ нарушений"
    )

Через Python API:
    from html_presentation_generator import generate_presentation
    path = generate_presentation(
        input_md="# Заголовок\\n\\n---\\n\\n## Слайд 2",
        output_path="/tmp/pres.html",
        title="Тест"
    )


Структура навыка
----------------

html_presentation_generator/
    __init__.py              — этот файл, публичный API
    tool.py                  — регистрация tool для nanobot (shim → scripts/tool.py)
    generate_presentation.bat — точка входа CLI (Windows)
    generate_presentation.sh  — точка входа CLI (Linux)
    SKILL.md                 — описание навыка (назначение, параметры, примеры)
    examples/
        input.md             — пример Markdown-файла с презентацией
        output.html          — пример сгенерированного HTML
    scripts/
        __init__.py          — публичный API пакета scripts
        cli.py               — CLI-точка входа
        generator.py         — parse_markdown_to_slides, generate_html
        tool.py              — HtmlPresentationTool для nanobot-агента
        templates/
            base.html        — HTML-шаблон (Mermaid CDN, навигация JS)
        assets/
            styles.css       — CSS (тёмная панель, @media print, адаптив)
"""

from typing import Optional

from .scripts.generator import parse_markdown_to_slides, generate_html as _generate_html
from .scripts.tool import HtmlPresentationTool

import os
import tempfile


def generate_presentation(
    input_md: str,
    output_path: Optional[str] = None,
    title: str = "Презентация",
) -> str:
    """
    Сгенерировать HTML-презентацию из Markdown (публичное API верхнего уровня).

    Args:
        input_md: Markdown-содержимое (слайды через ---).
        output_path: Путь к выходному HTML (если None — временный файл).
        title: Заголовок презентации.

    Returns:
        Путь к сгенерированному HTML-файлу.

    Пример:
        >>> path = generate_presentation(
        ...     "# Доклад\\n\\n---\\n\\n## Слайд 2\\nТекст",
        ...     title="Тест"
        ... )
        >>> path.endswith('.html')
        True
        >>> os.path.getsize(path) > 0
        True
    """
    slides = parse_markdown_to_slides(input_md)
    if not slides:
        raise ValueError("Не найдено слайдов. Убедитесь, что контент содержит --- разделители.")

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="presentation_")
        os.close(fd)

    # Пути к ресурсам (относительно этого файла)
    base = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(base, "scripts", "templates", "base.html")
    css_path = os.path.join(base, "scripts", "assets", "styles.css")

    for name, path in [("template", template_path), ("css", css_path)]:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Файл {name} не найден: {path}")

    return _generate_html(slides, template_path, css_path, output_path, title)


__all__ = [
    "generate_presentation",
    "HtmlPresentationTool",
    "parse_markdown_to_slides",
]
