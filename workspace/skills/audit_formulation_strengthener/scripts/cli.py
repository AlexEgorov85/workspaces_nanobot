"""Точка входа: CLI с разбором аргументов и маршрутизацией по режимам.

CLI — единая точка вызова навыка из shell/runtime/тестов. Он НЕ содержит
бизнес-логики режимов: делегирует в ``modes.analyze``, ``modes.search``,
``modes.synthesize`` (реализуются на следующих этапах).

Modes:
    analyze      — нормализация формулировки отклонения (1 LLM-вызов)
    search       — поиск релевантных фрагментов ВНД (N LLM-вызовов)
    synthesize   — финальный отчёт на русском юридическом языке
    all          — analyze → search → synthesize за один запуск
                   (с --confirm для длинных ВНД)

Примеры запуска::

    # Полный пайплайн
    python scripts/cli.py \\
        --violation "Срок хранения ПДн установлен 1 год" \\
        --vnd vnd1.pdf --vnd vnd2.docx \\
        --output report.md

    # Только нормализация отклонения
    python scripts/cli.py --mode analyze \\
        --violation "Срок хранения ПДн установлен 1 год"

    # Только финальный отчёт (кэшированные результаты analyze/search)
    python scripts/cli.py --mode synthesize \\
        --violation "..." --vnd vnd1.pdf \\
        --analyze-result /tmp/analyze.json --search-result /tmp/search.json

    # Оценка без LLM
    python scripts/cli.py --mode all \\
        --violation "..." --vnd vnd1.pdf --estimate-only

Из output идёт JSON в stdout для ``analyze``/``search``/``synthesize``
(если ``--internal-format json``), либо готовый текстовый отчёт для
``synthesize``/``all`` (по умолчанию).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Подключаем корень репо и scripts/ skill'а, чтобы sibling-модули
# (``modes.*``, ``report.*``, ``skill_config``, ``llm``, ``output``)
# импортировались и без выставленного PYTHONPATH.
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
_PROJECT_ROOT = str(_SKILL_ROOT.parents[1])
for _p in (_PROJECT_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import output as _output  # noqa: E402
from skill_config import get_cli_config  # noqa: E402


MODES = ("analyze", "search", "synthesize", "all")
OUTPUT_FORMATS = ("md", "docx", "txt")
INTERNAL_FORMATS = ("json", "report")


_SKILL_NAME = "audit_formulation_strengthener"


def _ensure_registered() -> None:
    """Standalone-CLI регистрирует skill в ``TableRegistry`` перед работой.

    В обычном runtime это делает ``ApplicationContext._auto_register_skills``
    (при старте ``gateway.py`` / ``cli_agent.py``). Для standalone-CLI
    без gateway — поднимаем самостоятельно, чтобы любые зависимости,
    ожидающие регистрации skill'а (например, ``get_predefined_scripts_table``
    или будущий vector-routing), находили запись. Идемпотентно: повторная
    регистрация игнорируется.

    Если ``config.py`` недоступен или ``SETTINGS`` не загрузился (например,
    при изолированном запуске для тестов) — WARN в stderr и продолжение:
    skill не зависит от TableRegistry на этапе 1 (у него нет таблиц и
    vector-индексов), и весь pipeline работает с файлами.
    """
    try:
        from config import SETTINGS  # type: ignore[import-not-found]
        from lib.core.skill_registration import (  # type: ignore[import-not-found]
            register_skill_from_config,
        )

        cfg = SETTINGS.get("skills", {}).get(_SKILL_NAME, {})
        register_skill_from_config(_SKILL_NAME, cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"[registration] WARN: {exc}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    cfg = get_cli_config()
    default_mode = cfg.get("default_mode", "all")
    default_timeout = int(cfg.get("timeout_sec", 120))

    parser = argparse.ArgumentParser(
        prog="audit_formulation_strengthener",
        description=(
            "Анализ формулировки отклонения по ВНД. "
            "Возвращает человекочитаемый отчёт (.md/.docx/.txt) "
            "в строгом русском юридическом стиле."
        ),
    )
    parser.add_argument(
        "--violation",
        required=True,
        help="Текст отклонения/нарушения, сформулированный аудитором.",
    )
    parser.add_argument(
        "--vnd",
        action="append",
        required=True,
        dest="vnd_paths",
        help=(
            "Путь к файлу ВНД (.pdf/.docx/.txt). "
            "Можно указать несколько раз для нескольких ВНД."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=default_mode,
        help=f"Режим работы (по умолчанию: {default_mode}).",
    )
    parser.add_argument(
        "--output-format",
        choices=OUTPUT_FORMATS,
        default="md",
        help="Формат финального отчёта (по умолчанию md).",
    )
    parser.add_argument(
        "--internal-format",
        choices=INTERNAL_FORMATS,
        default="report",
        help=(
            "Формат вывода для режимов analyze/search/synthesize. "
            "'report' — человекочитаемый отчёт (по умолчанию), "
            "'json' — машиночитаемый JSON."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Путь к файлу-результату. Если не указан — "
            "stdout (для отчёта) либо JSON в stdout."
        ),
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="Только оценить размер ВНД и время, без LLM-вызовов.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Подтвердить выполнение для длинных ВНД.",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Override max_chunks_for_execution из project.json.",
    )
    parser.add_argument(
        "--analyze-result",
        default=None,
        help=(
            "Путь к кэшированному JSON результата --mode analyze "
            "(используется --mode synthesize при resume)."
        ),
    )
    parser.add_argument(
        "--search-result",
        default=None,
        help=(
            "Путь к кэшированному JSON результата --mode search "
            "(используется --mode synthesize при resume)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=default_timeout,
        help=f"Таймаут на одну операцию в секундах (по умолчанию {default_timeout}).",
    )
    return parser


def _emit(payload: Any, *, target_path: str | None, is_report: bool) -> None:
    """Вывести результат: либо в файл, либо в stdout.

    Для отчёта (text/markdown) печатает как plain text.
    Для JSON — сериализует через json.dumps с ensure_ascii=False.
    """
    if target_path:
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_report:
            path.write_text(str(payload), encoding="utf-8")
        else:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return

    if is_report:
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_analyze(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Режим analyze. Возвращает (json-результат, текст-отчёт-or-None)."""
    # Реализуется на этапе 4
    from modes import analyze as analyze_mode  # noqa: WPS433

    return analyze_mode.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        estimate_only=args.estimate_only,
    )


def _run_search(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Режим search."""
    from modes import search as search_mode  # noqa: WPS433

    return search_mode.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result_path=args.analyze_result,
        estimate_only=args.estimate_only,
        confirm=args.confirm,
        max_chunks=args.max_chunks,
    )


def _run_search_with_analyze(
    args: argparse.Namespace,
    analyze_result: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Режим search с уже готовым результатом analyze (in-memory)."""
    from modes import search as search_mode  # noqa: WPS433

    return search_mode.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result=analyze_result,
        estimate_only=args.estimate_only,
        confirm=args.confirm,
        max_chunks=args.max_chunks,
    )


def _run_synthesize(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Режим synthesize. Возвращает (json, текст-отчёта)."""
    from modes import synthesize as synth_mode  # noqa: WPS433

    return synth_mode.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result_path=args.analyze_result,
        search_result_path=args.search_result,
        output_format=args.output_format,
        output_path=args.output,
    )


def _run_all(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Полный пайплайн: analyze → search → synthesize."""
    # analyze (этап 4) — реальный LLM.
    analyze_result, _ = _run_analyze(args)
    if analyze_result.get("status") != "success":
        return analyze_result, None

    # search (этап 5) — map-reduce с уже готовым analyze.
    search_result, _ = _run_search_with_analyze(args, analyze_result)
    if search_result.get("status") != "success":
        return search_result, None

    # Синтез — отдельным вызовом (передаём in-memory JSON).
    from modes import synthesize as synth_mode  # noqa: WPS433

    return synth_mode.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result=analyze_result,
        search_result=search_result,
        output_format=args.output_format,
        output_path=args.output,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Регистрация skill в TableRegistry (для standalone-CLI; в runtime
    # это делает ApplicationContext._auto_register_skills).
    _ensure_registered()

    # Валидация vnd_paths: должны существовать
    missing: list[str] = []
    for p in args.vnd_paths:
        if not Path(p).exists():
            missing.append(p)
    if missing:
        err = _output.make_error(
            f"Не найдены файлы ВНД: {', '.join(missing)}",
            error_type="vnd_not_found",
        )
        _emit(err, target_path=args.output, is_report=False)
        return 2

    try:
        if args.mode == "analyze":
            result, report_text = _run_analyze(args)
        elif args.mode == "search":
            result, report_text = _run_search(args)
        elif args.mode == "synthesize":
            result, report_text = _run_synthesize(args)
        elif args.mode == "all":
            result, report_text = _run_all(args)
        else:
            err = _output.make_error(
                f"Неизвестный режим: {args.mode}",
                error_type="unknown_mode",
            )
            _emit(err, target_path=args.output, is_report=False)
            return 2
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        err = _output.make_error(
            f"Внутренняя ошибка: {exc!r}\n{tb}",
            error_type="internal_error",
        )
        print(err, file=sys.stderr)
        _emit(err, target_path=args.output, is_report=False)
        return 1

    # Решаем, что отдавать пользователю
    if report_text is not None and args.internal_format == "report":
        _emit(report_text, target_path=args.output, is_report=True)
    else:
        _emit(result, target_path=args.output, is_report=False)

    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
