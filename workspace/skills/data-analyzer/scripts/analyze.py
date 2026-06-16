#!/usr/bin/env python3
"""
Главный CLI-маршрутизатор навыка.
Управляет выбором режима, сбором файлов и генерацией отчетов.
"""
import sys
import os
import argparse
import json

# Принудительно включаем UTF-8 для предотвращения краша на Windows cp1251
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

from utils import load_config, filter_files_by_extension
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def collect_files(path: str | None, files_list: list[str] | None, exts: list[str]) -> list[str]:
    """Собирает все файлы из указанного пути (рекурсивно) и/или из переданного списка,
    затем фильтрует по расширениям. Возвращает список путей к найденным файлам."""
    collected = []
    if path and os.path.isdir(path):
        for root, _, fnames in os.walk(path):
            collected.extend(os.path.join(root, f) for f in fnames)
    if files_list:
        collected.extend(files_list)
    return filter_files_by_extension(collected, exts)

def main():
    """CLI-маршрутизатор: разбирает аргументы командной строки, загружает конфиг,
    собирает целевые файлы, выбирает режим (llm_text/pandas), генерирует отчёт
    в формате md или json и выводит либо сохраняет результат."""
    parser = argparse.ArgumentParser(description="Folder Analyzer Skill (LLM Text & Pandas Modes)")
    parser.add_argument("--mode", choices=["llm_text", "pandas"], required=True,
                        help="Режим анализа: llm_text (текст/логи) или pandas (таблицы)")
    parser.add_argument("--path", type=str, help="Путь к папке для анализа")
    parser.add_argument("--files", type=str, nargs="+", help="Список конкретных файлов")
    parser.add_argument("--ext", type=str, nargs="+", default=[".txt", ".log", ".csv", ".json", ".xlsx"],
                        help="Фильтр по расширениям файлов")
    parser.add_argument("--question", type=str, required=True, help="Вопрос для анализа данных")
    parser.add_argument("--report", choices=["md", "json"], default="md", help="Формат выходного отчета")
    parser.add_argument("--output", type=str, help="Путь для сохранения отчета в файл")
    args = parser.parse_args()

    if not args.path and not args.files:
        print("[ERROR] Укажите хотя бы один аргумент: --path или --files.", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    target_files = collect_files(args.path, args.files, args.ext)

    if not target_files:
        print("[ERROR] Файлы не найдены или не соответствуют фильтру расширений.", file=sys.stderr)
        sys.exit(1)

    answer = ""
    try:
        if args.mode == "llm_text":
            from llm_text_analyzer import analyze_text_mode
            answer = analyze_text_mode(target_files, args.question, config)
        elif args.mode == "pandas":
            if len(target_files) > 1:
                print("[WARN] Режим pandas обрабатывает только первый файл из списка.", file=sys.stderr)
            from python_analyzer import analyze_pandas_mode
            answer = analyze_pandas_mode(target_files[0], args.question, config)
    except Exception as e:
        answer = f"[ERROR] Критическая ошибка выполнения режима {args.mode}: {e}"
        print(answer, file=sys.stderr)

    report_md = f"# Анализ: {args.mode}\n\n## Вопрос\n{args.question}\n\n## Ответ\n{answer}"
    report_json = json.dumps({
        "mode": args.mode,
        "question": args.question,
        "files_processed": target_files,
        "answer": answer
    }, ensure_ascii=False, indent=2)

    final_report = report_md if args.report == "md" else report_json

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(final_report)
        print(f"[INFO] Отчет сохранен в: {args.output}", file=sys.stderr)
    else:
        print(final_report)

if __name__ == "__main__":
    main()
