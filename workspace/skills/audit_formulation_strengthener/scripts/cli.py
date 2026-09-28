"""CLI entry point для skill'а ``audit_formulation_strengthener``.

Единственный путь запуска из shell/runtime/тестов.

Modes:

* ``analyze`` — нормализация формулировки отклонения (1 LLM-вызов).
* ``search`` — поиск релевантных фангов ВНД (N LLM-вызовов).
* ``synthesize`` — финальный отчёт (1 LLM-вызов).
* ``all`` — analyze → search → synthesize за один запуск.

Exit-коды:

* ``0`` — успех.
* ``1`` — ошибки обработки (``llm_error``, ``json_parse_failed``,
  ``prompt_missing``, ``prompt_unresolved_var``, ``schema_mismatch``,
  ``io_error``, ``internal_error``).
* ``2`` — ошибки входных данных (``no_vnd``, ``vnd_not_found``,
  ``vnd_unreadable``, ``vnd_empty``, ``too_many_chunks``,
  ``empty_violation``, ``analyze_result_unreadable``,
  ``search_result_unreadable``, ``unknown_mode``).

Поведенческие решения:

* ``--estimate-only + --output`` → ``--output`` игнорируется (нет
  артефакта — нет файла).
* Пустой ``--vnd`` → JSON ``no_vnd`` в stdout + exit 2 (а не
  ``argparse.SystemExit(2)`` с usage в stderr).
* При записи в файл через ``--output`` в stdout также печатается краткий
  JSON ``{mode, status, saved_to}`` для наблюдаемости.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Единственная точка модификации sys.path в production-коде скилла.
# Добавляем корень репо, чтобы пакетные импорты ``workspace.*`` и ``lib.*``
# резолвились без выставленного PYTHONPATH.
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = str(_SKILL_ROOT.parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workspace.skills.audit_formulation_strengthener.scripts.output import (
    make_error,
)
from workspace.skills.audit_formulation_strengthener.scripts.skill_config import (
    get_cli_config,
)


_MODES_PACKAGE = "workspace.skills.audit_formulation_strengthener.scripts.modes"


MODES = ("analyze", "search", "synthesize", "all")
OUTPUT_FORMATS = ("md", "docx", "txt")
INTERNAL_FORMATS = ("json", "report")

_SKILL_NAME = "audit_formulation_strengthener"


def _ensure_registered() -> None:
    """Standalone-CLI регистрирует skill в ``TableRegistry``.

    В обычном runtime это делает ``ApplicationContext._auto_register_skills``.
    Идемпотентно.

    ``SETTINGS`` — ``_LazySettings`` proxy, публикуемый через
    ``config._initialize_settings(profile)`` (change ``config-profile-cli-flag``).
    В standalone-CLI нет entrypoint'а, который бы его вызвал, поэтому
    инициализируем здесь (default = test, тот же паттерн, что в других
    standalone-CLI скиллов). Если proxy уже инициализирован (CLI внутри
    gateway/cli_agent) — no-op.
    """
    try:
        import config as _cfg  # type: ignore[import-not-found]

        if not _cfg.is_settings_initialized():
            _cfg._initialize_settings(profile="test")
    except Exception:  # noqa: BLE001
        pass  # работа с файлами ВНД возможна и без настроенного профиля
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
    cli_cfg = get_cli_config()
    default_mode = cli_cfg.get("default_mode", "all")

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
        required=False,  # НЕ required — пустой список → JSON no_vnd + exit 2
        default=[],
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
        help="Формат финального отчёта при --output (по умолчанию md).",
    )
    parser.add_argument(
        "--internal-format",
        choices=INTERNAL_FORMATS,
        default="report",
        help=(
            "Формат вывода для режима synthesize (и только его): "
            "'report' — человекочитаемый markdown (по умолчанию), "
            "'json' — машиночитаемый JSON. "
            "Для analyze/search всегда JSON (report_text=None)."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Путь к файлу-результату. Если не указан — stdout. "
            "Игнорируется вместе с --estimate-only (оценка только в stdout)."
        ),
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help=(
            "Только оценить размер/число LLM-вызовов без реальных вызовов. "
            "Работает во ВСЕХ режимах без LLM (analyze=1, search=N, "
            "synthesize=1+N+1, all=synthesize-form). "
            "В комбинации с --output флаг --output игнорируется."
        ),
    )
    parser.add_argument(
        "--analyze-result",
        default=None,
        help=(
            "Путь к кэшированному JSON результата --mode analyze "
            "(используется --mode search/synthesize при resume)."
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
    return parser


def _emit(
    payload: Any,
    *,
    kind: str,
    target_path: str | None,
    mode: str,
) -> None:
    """Записать результат в файл или stdout.

    Args:
        payload: что писать (str для report, dict для JSON).
        kind: ``"report"`` (plain text) или ``"json"``.
        target_path: путь к файлу; ``None`` → stdout.
        mode: имя режима (для краткого вывода после записи файла).
    """
    is_report = kind == "report"
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


def _emit_brief(mode: str, saved_to: str) -> None:
    """Краткий ``{mode, status, saved_to}`` в stdout после записи файла через ``--output``."""
    brief = {"mode": mode, "status": "success", "saved_to": saved_to}
    print(json.dumps(brief, ensure_ascii=False))


def _run_analyze(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Режим analyze. Возвращает (json-результат, текст-отчёта-or-None)."""
    from workspace.skills.audit_formulation_strengthener.scripts.modes import analyze

    return analyze.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths) if args.vnd_paths else None,
        estimate_only=args.estimate_only,
    )


def _run_search(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Режим search."""
    from workspace.skills.audit_formulation_strengthener.scripts.modes import search

    return search.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result_path=args.analyze_result,
        estimate_only=args.estimate_only,
    )


def _run_search_with_analyze(
    args: argparse.Namespace,
    analyze_result: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Режим search с уже готовым результатом analyze (in-memory)."""
    from workspace.skills.audit_formulation_strengthener.scripts.modes import search

    return search.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result=analyze_result,
        estimate_only=args.estimate_only,
    )


def _run_synthesize(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Режим synthesize. Возвращает (json, текст-отчёта)."""
    from workspace.skills.audit_formulation_strengthener.scripts.modes import synthesize

    return synthesize.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths) if args.vnd_paths else None,
        analyze_result_path=args.analyze_result,
        search_result_path=args.search_result,
        output_format=args.output_format,
        output_path=args.output,
        estimate_only=args.estimate_only,
    )


def _run_all(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Полный пайплайн: analyze → search → synthesize."""
    # 1) analyze
    analyze_result, _ = _run_analyze(args)
    if analyze_result.get("status") != "success":
        return analyze_result, None

    # 2) search (in-memory analyze_result)
    search_result, _ = _run_search_with_analyze(args, analyze_result)
    if search_result.get("status") != "success":
        return search_result, None

    # 3) synthesize
    from workspace.skills.audit_formulation_strengthener.scripts.modes import synthesize

    return synthesize.run(
        violation=args.violation,
        vnd_paths=list(args.vnd_paths),
        analyze_result=analyze_result,
        search_result=search_result,
        output_format=args.output_format,
        output_path=args.output,
        estimate_only=args.estimate_only,
    )


def main(argv: list[str] | None = None) -> int:
    # Инициализация SETTINGS должна произойти ДО _build_parser():
    # get_cli_config() читает project.json через ленивый proxy.
    _ensure_registered()
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Пустой --vnd → JSON no_vnd в stdout + exit 2
    # (а не argparse.SystemExit(2) с usage в stderr).
    if not args.vnd_paths and not args.estimate_only:
        # Для estimate-only без --vnd нужно дать каждому режиму шанс
        # вернуть свою оценку (analyze = без vnd, search/synthesize = no_vnd).
        # Поэтому валидацию пустого vnd делаем по режиму:
        if args.mode in ("search", "synthesize", "all"):
            err = make_error("Не указаны файлы ВНД", error_type="no_vnd")
            _emit(err, kind="json", target_path=None, mode=args.mode)
            return 2

    # Несуществующие файлы ВНД → JSON vnd_not_found + exit 2 ДО любого
    # LLM-вызова (иначе в режиме all ошибка всплыла бы только после
    # analyze, потратив вызов и замаскировавшись под llm_error).
    if args.vnd_paths:
        missing = [p for p in args.vnd_paths if not Path(p).exists()]
        if missing:
            err = make_error(
                f"Не найдены файлы ВНД: {', '.join(missing)}",
                error_type="vnd_not_found",
            )
            _emit(err, kind="json", target_path=None, mode=args.mode)
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
            err = make_error(
                f"Неизвестный режим: {args.mode}",
                error_type="unknown_mode",
            )
            _emit(err, kind="json", target_path=None, mode=args.mode)
            return 2
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        err = make_error(
            f"Внутренняя ошибка: {exc!r}\n{tb}",
            error_type="internal_error",
        )
        print(err, file=sys.stderr)
        _emit(err, kind="json", target_path=None, mode=args.mode)
        return 1

    # --estimate-only + --output → --output игнорируется
    # (нет артефакта — нет файла).
    output_target = None if args.estimate_only else args.output

    status = result.get("status", "error")

    # Если synthesize уже записал файл (md/txt/docx) — saved_to в result.
    # В этом случае печатаем только краткий JSON в stdout и выходим.
    if status == "success" and "saved_to" in result:
        _emit_brief(args.mode, result["saved_to"])
        return 0

    # Определяем, что эмитить.
    if (
        not args.estimate_only
        and report_text is not None
        and args.internal_format == "report"
    ):
        emit_kind = "report"
    else:
        emit_kind = "json"

    # Пишем в файл / stdout.
    _emit(
        report_text if emit_kind == "report" else result,
        kind=emit_kind,
        target_path=output_target,
        mode=args.mode,
    )

    # При записи в файл — краткий JSON в stdout для наблюдаемости.
    if status == "success" and output_target is not None:
        _emit_brief(args.mode, str(Path(output_target).resolve()))

    return 0 if status == "success" else _exit_code_for(result)


# error_type → exit code (2 = входные данные, 1 = обработка).
_INPUT_ERROR_TYPES = frozenset({
    "no_vnd",
    "vnd_not_found",
    "vnd_unreadable",
    "vnd_empty",
    "too_many_chunks",
    "empty_violation",
    "analyze_result_unreadable",
    "search_result_unreadable",
    "unknown_mode",
})


def _exit_code_for(result: dict[str, Any]) -> int:
    """Определить exit code по error_type в результате.

    По умолчанию ошибки обработки → exit 1, ошибки входных данных → exit 2.
    """
    if result.get("status") == "success":
        return 0
    err_type = str((result.get("data") or {}).get("error_type") or "")
    return 2 if err_type in _INPUT_ERROR_TYPES else 1


if __name__ == "__main__":
    raise SystemExit(main())
