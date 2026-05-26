"""
Ядро генератора HTML-презентаций из Markdown.

Содержит две основные функции:
    parse_markdown_to_slides — парсинг Markdown с разделителями ---
    generate_html          — сборка самодостаточного HTML из шаблона + CSS + слайдов

Зависимости: markdown, BeautifulSoup4.

Пример использования через код:
    >>> from generator import parse_markdown_to_slides, generate_html
    >>> md = "# Заголовок\\n\\n---\\n\\n## Слайд 2\\nТекст"
    >>> slides = parse_markdown_to_slides(md)
    >>> len(slides)
    2
    >>> slides[0]["title"]
    'Заголовок'

    >>> out = generate_html(slides, "templates/base.html", "assets/styles.css",
    ...                     "output.html", title="Тест")
    >>> out
    'output.html'

Пример запуска через CLI:
    generate_presentation.bat --input examples/input.md --output out.html --title "Мой доклад"
"""

import markdown
import re
import os
from bs4 import BeautifulSoup


def parse_markdown_to_slides(md_content: str) -> list[dict]:
    """
    Разделить Markdown-контент на слайды по разделителю '---'.

    Каждый слайд — dict с ключами:
        title:   первый # Заголовок (или None)
        content: остальной Markdown-текст (кроме заголовка #)
        mermaid: код Mermaid-диаграммы между ```mermaid и ``` (или None)

    Разделитель --- должен быть на отдельной строке (с обеих сторон).

    Args:
        md_content: Markdown-текст с несколькими слайдами.

    Returns:
        Список слайдов.

    Пример:
        >>> slides = parse_markdown_to_slides(
        ...     "# Титул\\n\\n---\\n\\n## Второй слайд\\nТекст\\n\\n"
        ...     "```mermaid\\npie title Test\\n\\\"A\\\" : 50\\n```"
        ... )
        >>> len(slides)
        2
        >>> slides[0]["title"]
        'Титул'
        >>> slides[1]["mermaid"] is not None
        True
        >>> "```" in slides[1]["mermaid"]
        False
    """
    slides = []
    slide_blocks = re.split(r'^---$', md_content, flags=re.MULTILINE)

    for block in slide_blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split('\n')
        title = None
        content = []
        mermaid = None

        # Поиск заголовка первого уровня (# ...)
        for line in lines:
            if line.startswith('# '):
                title = line[2:].strip()
                break

        mermaid_block = False
        mermaid_code = []
        for line in lines:
            if line.startswith('```mermaid'):
                mermaid_block = True
                continue
            elif line.startswith('```') and mermaid_block:
                mermaid_block = False
                mermaid = '\n'.join(mermaid_code)
                mermaid_code = []
                continue

            if mermaid_block:
                mermaid_code.append(line)
            elif not line.startswith('# '):
                content.append(line)

        slides.append({
            'title': title,
            'content': '\n'.join(content),
            'mermaid': mermaid
        })

    return slides


def generate_html(
    slides: list[dict],
    template_path: str,
    css_path: str,
    output_path: str,
    title: str = "Презентация",
) -> str:
    """
    Собрать самодостаточный HTML-файл из шаблона, CSS и слайдов.

    Pipeline:
        1. Загрузить HTML-шаблон (templates/base.html)
        2. Загрузить CSS (assets/styles.css)
        3. Конвертировать Markdown-контент каждого слайда в HTML (с таблицами)
        4. Вставить заголовок <title>
        5. Вставить <style> с CSS
        6. Для каждого слайда: div.slide > div.slide-content > {h1, div.content, div.mermaid}
        7. Сохранить результат в output_path

    Args:
        slides:          Список слайдов (от parse_markdown_to_slides).
        template_path:   Путь к base.html.
        css_path:        Путь к styles.css.
        output_path:     Куда сохранить результат.
        title:           Заголовок презентации (тег <title>).

    Returns:
        output_path (путь к сгенерированному файлу).

    Пример:
        >>> slides = [{"title": "Слайд 1", "content": "**Привет**", "mermaid": None}]
        >>> generate_html(slides,
        ...     "templates/base.html", "assets/styles.css",
        ...     "/tmp/test_output.html", title="Тест")  # doctest: +SKIP
        '/tmp/test_output.html'
    """
    with open(template_path, 'r', encoding='utf-8') as f:
        template_html = f.read()

    with open(css_path, 'r', encoding='utf-8') as f:
        css_content = f.read()

    # Конвертация Markdown → HTML для каждого слайда
    for slide in slides:
        if slide['content']:
            slide['content'] = markdown.markdown(slide['content'], extensions=['tables'])

    soup = BeautifulSoup(template_html, 'html.parser')

    # Заголовок
    title_tag = soup.find('title')
    if title_tag:
        title_tag.string = title

    # Встроенный CSS
    style_tag = soup.new_tag('style')
    style_tag.string = css_content
    soup.head.append(style_tag)

    # Контейнер слайдов
    container = soup.find(id='slides-container')
    if not container:
        container = soup.find('body')

    # Наполнение слайдами
    for slide in slides:
        slide_div = soup.new_tag('div', **{'class': 'slide'})
        content_div = soup.new_tag('div', **{'class': 'slide-content'})

        if slide['title']:
            h1 = soup.new_tag('h1')
            h1.string = slide['title']
            content_div.append(h1)

        if slide['content']:
            inner = soup.new_tag('div', **{'class': 'content'})
            inner.append(BeautifulSoup(slide['content'], 'html.parser'))
            content_div.append(inner)

        if slide['mermaid']:
            mermaid_div = soup.new_tag('div', **{'class': 'mermaid'})
            mermaid_div.string = slide['mermaid']
            content_div.append(mermaid_div)

        slide_div.append(content_div)
        container.append(slide_div)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(str(soup))

    return output_path
