"""
Tool-класс HtmlPresentationTool для nanobot-агента.

Регистрирует tool **generate_presentation**, который генерирует
самодостаточные HTML-презентации из Markdown с поддержкой:
    - Разделение слайдов ---
    - Mermaid-диаграммы (```mermaid ... ```)
    - Таблицы, списки, жирный текст
    - Навигация клавишами ← → Space F Home End
    - Прогресс-бар и кнопки управления
    - Адаптивный дизайн и печать

Вызов из nanobot-агента:
    generate_presentation(input="...", output="out.html", title="Мой доклад")
    generate_presentation(input_file="slides.md", output="out.html")
"""

import os
import time
import traceback

from nanobot.agent.tools.base import Tool, tool_parameters

from generator import generate_html, parse_markdown_to_slides


def _tool_dir() -> str:
    """
    Абсолютный путь к директории scripts/.

    Returns:
        Строка пути.

    Пример:
        >>> _tool_dir().endswith('scripts')
        True
    """
    return os.path.dirname(os.path.abspath(__file__))


@tool_parameters({
    "type": "object",
    "properties": {
        "input": {
            "type": "string",
            "description": (
                "Markdown-содержимое презентации. Слайды разделяются тремя дефисами ---. "
                "Поддерживает: заголовки, текст, таблицы, графики Mermaid (```mermaid ... ```). "
                "Первый # заголовок становится заголовком слайда."
            ),
        },
        "input_file": {
            "type": "string",
            "description": "Путь к Markdown-файлу (альтернатива input). Если указан, input игнорируется.",
        },
        "output": {
            "type": "string",
            "description": (
                "Путь к выходному HTML-файлу. Если не указан, "
                "сохраняется в workspace/output/presentations/."
            ),
        },
        "title": {
            "type": "string",
            "description": "Заголовок презентации (тег <title>). По умолчанию: 'Презентация'",
        },
    },
    "required": [],
})
class HtmlPresentationTool(Tool):
    """
    Генератор HTML-презентаций из Markdown.

    Атрибуты класса:
        name: "generate_presentation" — имя для вызова из агента.
        description: Описание для LLM (что делает tool).

    Агент может вызвать:
        generate_presentation(input="...")       — передать Markdown напрямую
        generate_presentation(input_file="...")  — передать путь к файлу
        generate_presentation(..., output="...", title="...") — доп. параметры

    Возвращает:
        Строку с путём к сгенерированному файлу и количеством слайдов.
    """

    name = "generate_presentation"
    description = (
        "Генерирует HTML-презентацию из Markdown-разметки. "
        "Принимает содержимое презентации (input) или путь к файлу (input_file). "
        "Слайды разделяются ---. "
        "Поддерживает таблицы, списки, Mermaid-диаграммы (```mermaid ... ```). "
        "Результат — самодостаточный HTML-файл с навигацией (клавиши ←/→/Space/F), "
        "прогресс-баром, адаптивным дизайном и печатью."
    )

    def __init__(self):
        self._name = "generate_presentation"

    def _make_result(self, message: str, file_path: str) -> str:
        """
        Форматирование результата для возврата агенту.

        Args:
            message: Текстовое сообщение.
            file_path: Путь к файлу (не используется в текущей реализации).

        Returns:
            То же сообщение.
        """
        return message

    async def execute(
        self,
        input: str | None = None,
        input_file: str | None = None,
        output: str | None = None,
        title: str | None = None,
    ) -> str:
        """
        Выполнить генерацию презентации.

        Pipeline:
            1. Получить Markdown-контент (из input или input_file)
            2. Распарсить на слайды (parse_markdown_to_slides)
            3. Загрузить шаблон (templates/base.html) и CSS (assets/styles.css)
            4. Если output не указан — создать в workspace/output/presentations/
            5. Собрать HTML (generate_html)
            6. Вернуть сообщение с путём и количеством слайдов

        Args:
            input: Markdown-содержимое презентации.
            input_file: Путь к Markdown-файлу (альтернатива input).
            output: Путь к выходному HTML (опционально).
            title: Заголовок презентации (опционально).

        Returns:
            Строка с результатом или ошибкой.

        Пример вызова агентом:
            generate_presentation(
                input="# Доклад\\n\\n---\\n\\n## Слайд 2\\nТекст",
                output="report.html",
                title="Годовой отчёт"
            )
        """
        try:
            if input_file:
                try:
                    with open(input_file, 'r', encoding='utf-8') as f:
                        md_content = f.read()
                except FileNotFoundError:
                    return f"Ошибка: файл не найден: {input_file}"
            elif input:
                md_content = input
            else:
                return "Ошибка: укажите input (содержимое) или input_file (путь к файлу)"

            title = title or "Презентация"

            slides = parse_markdown_to_slides(md_content)
            if not slides:
                return "Ошибка: не найдено слайдов. Убедитесь, что контент содержит слайды, разделённые ---"

            td = _tool_dir()
            template_path = os.path.join(td, 'templates', 'base.html')
            css_path = os.path.join(td, 'assets', 'styles.css')

            for name, path in [("template", template_path), ("css", css_path)]:
                if not os.path.isfile(path):
                    return f"Ошибка: файл {name} не найден: {path}"

            if not output:
                output_dir = os.path.join(
                    os.path.dirname(os.path.dirname(_tool_dir())),
                    "output", "presentations"
                )
                os.makedirs(output_dir, exist_ok=True)
                output = os.path.join(
                    output_dir,
                    f"presentation_{int(time.time())}_{id(self)}.html"
                )

            output_dir = os.path.dirname(os.path.abspath(output))
            os.makedirs(output_dir, exist_ok=True)

            out_path = generate_html(slides, template_path, css_path, output, title)

            msg = (
                f"Презентация успешно сгенерирована: {out_path}\n"
                f"Слайдов: {len(slides)}\n"
                f"Навигация: <- -> Space, F — полный экран, Home/End — начало/конец."
            )
            return self._make_result(msg, out_path)

        except Exception as e:
            return f"Ошибка генерации презентации: {e}\n\n{traceback.format_exc()}"
