"""
Точка входа: CLI с разбором аргументов и маршрутизацией по режимам.

Режимы:
    predefined     — выполнение готовых SQL-шаблонов (--script + --params)
    generated_sql  — генерация SQL через LLM по текстовому запросу (--query)
    vector         — семантический поиск по FAISS-индексу (--query + --index-name, --top-k/--threshold)

Примеры запуска:
    # Предопределённый скрипт
    audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'

    # Генерация SQL через LLM
    audit_analyze --mode generated_sql --query 'сколько аудитов было в 2024 по месяцам'

    # Векторный поиск: топ-3
    audit_analyze --mode vector --query 'пожарная безопасность' --index-name audits_index --top-k 3

    # Векторный поиск: всё выше порога 0.5
    audit_analyze --mode vector --query 'статусы аудитов' --index-name audits_index --threshold 0.5

    # С контекстом чата (история для LLM в generated_sql-режиме)
    audit_analyze --mode generated_sql --query 'покажи детали' \\
        --context '[{"role":"user","content":"привет"}]'
"""

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any


def _parse_params(raw: str) -> dict[str, Any]:
    """
    Распарсить --params в dict. Поддерживает два формата:

    1. JSON:  {"year": 2024, "limit": 10}
    2. key=value через запятую:  year=2024, limit=10

    Args:
        raw: Строка параметров.

    Returns:
        dict с параметрами.

    Пример:
        >>> _parse_params('{"year": 2024}')
        {'year': 2024}
        >>> _parse_params('year=2024')
        {'year': '2024'}
        >>> _parse_params('year=2024,limit=10')
        {'year': '2024', 'limit': '10'}
    """
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise argparse.ArgumentTypeError(f"Неверный JSON в --params: {e}") from e
    result: dict[str, Any] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        result[k.strip()] = v.strip()
    return result

# Add scripts dir to path so sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

import predefined_mode  # noqa: E402
import generated_sql_mode  # noqa: E402
from output import _sanitize_value, prepare_output  # noqa: E402
from skill_config import (  # noqa: E402
    build_cache_provider,
    get_in_memory_cache_path,
    get_vector_index_path,
)


def _ensure_registered() -> None:
    """Зарегистрировать audit_analyzer и runtime-инфраструктуру в ``table_registry``.

    Standalone CLI не имеет ``ApplicationContext`` (его поднимает gateway),
    поэтому skill сам себя регистрирует из ``project.json::skills.audit_analyzer``
    через ``lib.core.skill_registration.register_skill_from_config``. Идемпотентно:
    если skill уже зарегистрирован (например, gateway-populated реестр),
    повторная регистрация игнорируется.

    Дополнительно поднимаем общую runtime-инфраструктуру (``vector.storage``
    + ``embedding_config``), без которой ``search_vector`` падает на
    «Не удалось получить эмбеддинг запроса»: эти ресурсы в обычном
    режиме кладёт ``ApplicationContext._register_infra_resources``.
    """
    from lib.core.infra_registration import register_vector_storage
    from lib.core.skill_registration import (
        register_embedding_config,
        register_skill_from_config,
    )
    from config import SETTINGS

    audit_cfg = SETTINGS.get("skills", {}).get("audit_analyzer", {})
    register_skill_from_config("audit_analyzer", audit_cfg)
    register_vector_storage()
    register_embedding_config()


def _build_parser() -> argparse.ArgumentParser:
    """
    Создать и настроить парсер аргументов командной строки.
    Добавляет ~10 аргументов: --mode, --script, --query,
    --params, --vector-index, --index-name, --top-k, --threshold, --context.
    Возвращает настроенный ArgumentParser.

    --mode не обязателен: значение по умолчанию берётся из
    ``skills.audit_analyzer.cli_default_mode`` в project.json.
    """
    from skill_config import get_cli_config

    default_mode = get_cli_config().get("default_mode", "predefined")
    parser = argparse.ArgumentParser(description="audit_analyzer — анализ БД через LLM агента")
    parser.add_argument(
        "--mode",
        default=default_mode,
        choices=["predefined", "generated_sql", "vector"],
        help=f"Режим работы: predefined, generated_sql или vector (default: {default_mode})",
    )
    parser.add_argument(
        "--script",
        default=None,
        help="Имя скрипта (обязательно для mode=predefined). "
             "Например: analytics_by_year_month, violations_by_type",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Запрос на естественном языке (для mode=generated_sql/vector). "
             "Например: 'сколько аудитов было в 2024'",
    )
    parser.add_argument(
        "--params",
        default=None,
        type=_parse_params,
        help='Параметры скрипта для mode=predefined. '
             'Форматы: \'{"year": 2024}\' (JSON) или year=2024 (key=value, запятые).',
    )
    parser.add_argument(
        "--index-name",
        default=None,
        help="Имя индекса для mode=vector. "
             "По умолчанию: 'audits_index'. "
             "Индекс живёт в DuckDB-кэше (workspace/data_store/duckdb/cache.duckdb), "
             "путь к .faiss-файлам не нужен.",
    )
    parser.add_argument(
        "--top-k",
        default=None,
        type=int,
        help="Количество результатов для mode=vector (по умолч. 5). "
             "Игнорируется при --threshold.",
    )
    parser.add_argument(
        "--threshold",
        default=None,
        type=float,
        help="Минимальный порог схожести (0.0–1.0) для mode=vector. "
             "Если задан, возвращаются ВСЕ результаты выше порога, --top-k игнорируется.",
    )
    parser.add_argument(
        "--context",
        default=None,
        type=json.loads,
        help='Контекст чата в формате JSON (опционально). '
             'Например: \'[{"role":"user","content":"привет"}]\'',
    )
    return parser


def _open_db():
    provider = build_cache_provider()
    cache_path = get_in_memory_cache_path()
    if not provider.open_cache():
        raise FileNotFoundError(
            f"DuckDB-кеш не найден: {cache_path}. "
            "Кеш создаёт и обновляет gateway автоматически — "
            "запустите его (python gateway.py)."
        )
    print(f"[DB] DuckDB in-memory cache ({cache_path})", file=sys.stderr)
    return provider


def _run(args: argparse.Namespace) -> dict:
    """Маршрутизация выполнения по режиму (predefined/generated_sql/vector)."""
    db = _open_db()
    try:
        if args.mode == "predefined":
            if not args.script:
                return {"status": "error", "data": {"message": "Для mode=predefined укажите --script"}}
            return predefined_mode.run(args.script, db, params=args.params,
                                        index_dir=get_vector_index_path())

        if not args.query:
            return {"status": "error", "data": {"message": f"Для mode={args.mode} требуется --query"}}

        if args.mode == "generated_sql":
            return generated_sql_mode.run(args.query, db, context=args.context)

        if args.mode == "vector":
            from dataclasses import asdict
            if not args.index_name:
                return {
                    "status": "error",
                    "data": {"message": "Для mode=vector укажите --index-name "
                                       "(audits_index / violations_index / audit_reports_index из skills.audit_analyzer.vector_indexes[])"}
                }
            results = db.search_vector(
                args.query,
                index_name=args.index_name,
                top_k=args.top_k or 5,
                threshold=args.threshold,
            )
            if getattr(db, "_search_error", None):
                return {"status": "error", "data": {"message": db._search_error}}
            if not results:
                return {
                    "status": "success",
                    "data": {"message": "Документы не найдены", "results": [], "count": 0},
                }
            return {
                "status": "success",
                "data": {"results": [asdict(r) for r in results], "count": len(results)},
            }

        return {"status": "error", "data": {"message": f"Неизвестный режим: {args.mode}"}}
    finally:
        db.close()


def main() -> None:
    """
    Точка входа скрипта (вызывается из audit_analyze.bat/.sh).

    Pipeline:
        1. Саморегистрация в ``table_registry`` (для standalone-режима
           без ``ApplicationContext``).
        2. Парсинг аргументов (argparse)
        3. Запуск _run() с маршрутизацией
        4. Форматирование результата (prepare_output)
        5. Вывод JSON в stdout

    Выходной JSON всегда содержит:
        - mode: режим работы
        - status: "success" | "error"
        - поля в зависимости от режима
    """
    try:
        _ensure_registered()
        parser = _build_parser()
        args = parser.parse_args()

        result = _run(args)
        output = _sanitize_value(prepare_output(result, args.mode))
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    except argparse.ArgumentTypeError as e:
        print(json.dumps({
            "mode": "unknown",
            "status": "error",
            "message": str(e),
        }, ensure_ascii=False, indent=2))
        sys.exit(1)
    except FileNotFoundError as e:
        print(json.dumps({
            "mode": "unknown",
            "status": "error",
            "message": str(e),
        }, ensure_ascii=False, indent=2))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({
            "mode": "unknown",
            "status": "error",
            "message": f"Внутренняя ошибка: {e}",
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
