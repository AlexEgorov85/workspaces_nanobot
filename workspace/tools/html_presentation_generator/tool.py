import os
import time
import traceback

from nanobot.agent.tools.base import Tool, tool_parameters

from .generator import generate_html, parse_markdown_to_slides


def _tool_dir():
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
            "description": "Путь к выходному HTML-файлу. Если не указан, сохраняется в workspace/output/presentations/.",
        },
        "title": {
            "type": "string",
            "description": "Заголовок презентации (тег <title>). По умолчанию: 'Презентация'",
        },
    },
    "required": [],
})
class HtmlPresentationTool(Tool):
    """Генератор HTML-презентаций из Markdown с поддержкой Mermaid, таблиц и навигации."""

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
        return message

    async def execute(
        self,
        input: str | None = None,
        input_file: str | None = None,
        output: str | None = None,
        title: str | None = None,
    ) -> str:
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
                output_dir = os.path.join(os.path.dirname(os.path.dirname(_tool_dir())), "output", "presentations")
                os.makedirs(output_dir, exist_ok=True)
                output = os.path.join(output_dir, f"presentation_{int(time.time())}_{id(self)}.html")

            output_dir = os.path.dirname(os.path.abspath(output))
            os.makedirs(output_dir, exist_ok=True)

            out_path = generate_html(slides, template_path, css_path, output, title)

            msg = (
                f"Презентация успешно сгенерирована: {out_path}\n"
                f"Слайдов: {len(slides)}\n"
                f"Навигация: ← → Space, F — полный экран, Home/End — начало/конец."
            )
            return self._make_result(msg, out_path)

        except Exception as e:
            return f"Ошибка генерации презентации: {e}\n\n{traceback.format_exc()}"
