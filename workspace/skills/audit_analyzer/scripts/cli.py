"""
Точка входа: CLI с разбором аргументов и маршрутизацией по режимам.

Режимы:
    predefined  — выполнение готовых SQL-шаблонов (--script + --params)
    sql         — генерация SQL через LLM по текстовому запросу (--query)
    vector      — семантический поиск по FAISS-индексу (--query + --index-name, --top-k/--threshold)

Примеры запуска:
    # Предопределённый скрипт
    audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'

    # SQL-генерация
    audit_analyze --mode sql --query 'сколько аудитов было в 2024 по месяцам'

    # Векторный поиск: топ-3
    audit_analyze --mode vector --query 'пожарная безопасность' --index-name audits_index --top-k 3

    # Векторный поиск: всё выше порога 0.5
    audit_analyze --mode vector --query 'статусы аудитов' --index-name audits_index --threshold 0.5

    # Векторный поиск с кастомной директорией индексов
    audit_analyze --mode vector --query 'финансы' --index-name fin_index \\
        --vector-index 'C:/custom/path'

    # С контекстом чата (история для LLM в sql-режиме)
    audit_analyze --mode sql --query 'покажи детали' \\
        --context '[{"role":"user","content":"привет"}]'
"""

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Optional


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
            raise argparse.ArgumentTypeError(f"Неверный JSON в --params: {e}")
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

from skill_config import (
    get_vector_index_path, get_max_retries, load_db_config,
    is_in_memory_enabled, get_in_memory_config, build_cache_provider,
)
from database import Database, QueryBackend
from output import _sanitize_value, prepare_output
import predefined_mode
import sql_mode


def _build_parser() -> argparse.ArgumentParser:
    """
    Создать и настроить парсер аргументов командной строки.
    Добавляет ~10 аргументов: --mode (обязательный), --script, --query,
    --params, --vector-index, --index-name, --top-k, --threshold, --context.
    Возвращает настроенный ArgumentParser.
    """
    parser = argparse.ArgumentParser(description="audit_analyzer — анализ БД через LLM агента")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["predefined", "sql", "vector", "init"],
        help="Режим работы: predefined, sql, vector или init",
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
        help="Запрос на естественном языке (для mode=sql/vector). "
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
        "--vector-index",
        default=None,
        help="Директория с FAISS-индексами (только для vector mode). "
             "По умолчанию из skills.audit_analyzer.mode_vector_index_path "
             "или ~/.nanobot/vectors/audits_index",
    )
    parser.add_argument(
        "--index-name",
        default=None,
        help="Имя индекса (без .faiss) для mode=vector. "
             "По умолчанию: 'audits_index'",
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
    parser.add_argument(
        "--force",
        default=False,
        action="store_true",
        help="Принудительная перезагрузка кеша (только для mode=init). "
             "Игнорировать существующий DuckDB-файл.",
    )
    return parser


def _create_db() -> QueryBackend:
    cfg = load_db_config()
    if is_in_memory_enabled():
        im_cfg = get_in_memory_config()
        provider = build_cache_provider()
        provider.open_cache()
        print(f"[DB] DuckDB in-memory cache ({im_cfg.get('cache_path', '?')})", file=sys.stderr)
        return provider
    print("[DB] PostgreSQL (direct)", file=sys.stderr)
    return Database(cfg)


def _run(args: argparse.Namespace) -> dict:
    """
    Маршрутизация выполнения по режиму (predefined/sql/vector/init).
    Для predefined проверяет наличие --script, для sql/vector — --query.
    Возвращает dict-результат от соответствующего модуля.
    """
    if args.mode == "init":
        cfg = load_db_config()
        im_cfg = get_in_memory_config()
        cache_path = im_cfg.get("cache_path", "")
        if not cache_path:
            return {"status": "error", "data": {"message": "in_memory.cache_path не задан в config.json"}}
        if not args.force and Path(cache_path).exists():
            return {"status": "success", "mode": "init", "data": {"message": "Кеш уже существует, используйте --force для перезагрузки"}}
        try:
            from lib.services.cache_provider_impl import load_cache_from_postgres
            load_cache_from_postgres(cache_path, cfg)
        except Exception as e:
            return {"status": "error", "data": {"message": f"Ошибка загрузки кеша: {e}"}}
        return {"status": "success", "mode": "init", "data": {"message": f"Кеш загружен: {cache_path}"}}

    with _create_db() as db:

        if args.mode == "predefined":
            if not args.script:
                return {"status": "error", "data": {"message": "Для mode=predefined укажите --script"}}
            return predefined_mode.run(args.script, db, params=args.params,
                                        index_dir=get_vector_index_path())

        if not args.query:
            return {"status": "error", "data": {"message": f"Для mode={args.mode} требуется --query"}}

        if args.mode == "sql":
            return sql_mode.run(args.query, db, context=args.context)

    if args.mode == "vector":
        from dataclasses import asdict
        provider = build_cache_provider()
        results = provider.search_vector(
            args.query,
            index_name=args.index_name or "audits_index",
            index_path=args.vector_index or get_vector_index_path(),
            top_k=args.top_k or 5,
            threshold=args.threshold,
        )
        if provider._search_error:
            return {"status": "error", "data": {"message": provider._search_error}}
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


def main() -> None:
    """
    Точка входа скрипта (вызывается из audit_analyze.bat/.sh).

    Pipeline:
        1. Парсинг аргументов (argparse)
        2. Запуск _run() с маршрутизацией
        3. Форматирование результата (prepare_output)
        4. Вывод JSON в stdout

    Выходной JSON всегда содержит:
        - mode: режим работы
        - status: "success" | "error"
        - поля в зависимости от режима
    """
    try:
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
