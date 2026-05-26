# -*- coding: utf-8 -*-
"""
html_presentation_generator.scripts — ядро генератора HTML-презентаций.

Предоставляет функции для парсинга Markdown-содержимого в слайды,
сборки самодостаточного HTML-файла, и tool-класс для nanobot-агента.


Структура пакета
----------------

scripts/
    __init__.py        — публичный API пакета
    cli.py             — CLI-точка входа (generate_presentation.bat)
    generator.py       — parse_markdown_to_slides, generate_html
    tool.py            — HtmlPresentationTool для nanobot-агента
    templates/
        base.html      — HTML-шаблон (с Mermaid CDN, навигацией, прогресс-баром)
    assets/
        styles.css     — адаптивный CSS (тёмная панель, печать, мобильная вёрстка)


Примеры использования через код
--------------------------------

    # Парсинг Markdown в слайды
    >>> from html_presentation_generator.scripts import parse_markdown_to_slides
    >>> slides = parse_markdown_to_slides(
    ...     "# Доклад\\n\\n---\\n\\n## Слайд 2\\nТекст слайда"
    ... )
    >>> len(slides)
    2

    # Генерация HTML
    >>> from html_presentation_generator.scripts import generate_html
    >>> path = generate_html(
    ...     slides,
    ...     "path/to/templates/base.html",
    ...     "path/to/assets/styles.css",
    ...     "/tmp/out.html",
    ...     title="Мой доклад"
    ... )
    >>> path
    '/tmp/out.html'

    # Через tool (вызов из агента)
    >>> from html_presentation_generator.scripts.tool import HtmlPresentationTool
    >>> tool = HtmlPresentationTool()
    >>> result = await tool.execute(
    ...     input="# Титул\\n\\n---\\n\\n## Второй\\nТекст",
    ...     output="/tmp/pres.html",
    ...     title="Тест"
    ... )
    >>> "успешно" in result
    True


Примеры запуска через CLI
-------------------------

    generate_presentation --input examples/input.md --output out.html
    generate_presentation --input slides.md --output report.html --title "Годовой отчёт"
"""

from generator import parse_markdown_to_slides, generate_html
from tool import HtmlPresentationTool, _tool_dir

__all__ = [
    "parse_markdown_to_slides",
    "generate_html",
    "HtmlPresentationTool",
    "_tool_dir",
]
