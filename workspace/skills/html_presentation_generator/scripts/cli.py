"""
CLI-точка входа для генерации HTML-презентаций из Markdown-файла.

Вызывается из generate_presentation.bat / .sh.

Примеры запуска:
    generate_presentation --input examples/input.md --output out.html
    generate_presentation --input slides.md --output report.html --title "Годовой отчёт"
"""

import argparse
import os
import sys

from generator import generate_html, parse_markdown_to_slides


def _resolve_tool_dir() -> str:
    """
    Абсолютный путь к директории scripts/.

    Возвращает:
        Путь к папке, где лежит этот скрипт.

    Пример:
        >>> _resolve_tool_dir().endswith('scripts')
        True
    """
    return os.path.dirname(os.path.abspath(__file__))


def main():
    """
    Точка входа CLI.

    Пайплайн:
        1. Парсинг аргументов: --input, --output, --title
        2. Проверка существования template и css файлов
        3. Чтение входного Markdown-файла
        4. Парсинг слайдов
        5. Генерация HTML
        6. Вывод пути к результату

    Аргументы:
        --input  : Путь к Markdown-файлу (обязательный).
        --output : Путь к выходному HTML (обязательный).
        --title  : Заголовок презентации (по умолчанию 'Презентация').

    Пример:
        python scripts/cli.py --input examples/input.md --output /tmp/out.html --title "Тест"
    """
    parser = argparse.ArgumentParser(description='Генерация HTML-презентаций из Markdown.')
    parser.add_argument('--input', required=True, help='Путь к входному Markdown-файлу')
    parser.add_argument('--output', required=True, help='Путь к выходному HTML-файлу')
    parser.add_argument('--title', default='Презентация', help='Заголовок презентации')
    args = parser.parse_args()

    tool_dir = _resolve_tool_dir()
    template_path = os.path.join(tool_dir, 'templates', 'base.html')
    css_path = os.path.join(tool_dir, 'assets', 'styles.css')

    for name, path in [('template', template_path), ('css', css_path)]:
        if not os.path.isfile(path):
            print(f"Ошибка: файл {name} не найден: {path}")
            sys.exit(1)

    try:
        with open(args.input, 'r', encoding='utf-8') as f:
            md_content = f.read()
    except FileNotFoundError:
        print(f"Ошибка: входной файл не найден: {args.input}")
        sys.exit(1)

    slides = parse_markdown_to_slides(md_content)

    if not slides:
        print("Ошибка: не найдено слайдов во входном файле")
        sys.exit(1)

    out = generate_html(slides, template_path, css_path, args.output, args.title)
    print(f"Презентация успешно сгенерирована: {out}")


if __name__ == '__main__':
    main()
