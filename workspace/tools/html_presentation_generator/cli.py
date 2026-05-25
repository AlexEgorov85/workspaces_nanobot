import argparse
import os
import sys

from .generator import generate_html, parse_markdown_to_slides


def _resolve_tool_dir():
    return os.path.dirname(os.path.abspath(__file__))


def main():
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
