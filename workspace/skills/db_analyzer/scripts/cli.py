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
import asyncio
import json
import sys
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
        return json.loads(raw)
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

from config import get_vector_index_path, get_max_retries, load_db_config
from database import Database
from output import _sanitize_value, prepare_output
import predefined_mode
import sql_mode
import vector_mode


def _build_parser() -> argparse.ArgumentParser:
    """
    Собрать парсер аргументов командной строки.

    Arguments:
        --mode: Режим работы (predefined | sql | vector) — обязательный.
        --script: Имя скрипта из SCRIPTS_REGISTRY (для predefined).
        --query: Запрос на естественном языке (для sql/vector).
        --params: JSON с параметрами скрипта (для predefined).
                  Парсится json.loads, передаётся как dict.
        --vector-index: Директория с FAISS-индексами (для vector).
        --index-name: Имя индекса без .faiss (для vector).
        --context: История чата в JSON (для sql/vector, опционально).

    Returns:
        argparse.ArgumentParser с настроенными аргументами.
    """
    parser = argparse.ArgumentParser(description="db_analyzer — анализ БД через LLM агента")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["predefined", "sql", "vector"],
        help="Режим работы: predefined, sql или vector",
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
             "По умолчанию из config.json modes.vector.index_path",
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
    return parser


async def _run(args: argparse.Namespace) -> dict:
    """
    Маршрутизация по режимам.

    В зависимости от args.mode вызывает соответствующий модуль:
        predefined → predefined_mode.run()
        sql        → sql_mode.run()
        vector     → vector_mode.run()

    Args:
        args: Распарсенные аргументы (argparse.Namespace).

    Returns:
        dict с результатом выполнения режима.
    """
    context = args.context

    async with Database(load_db_config()) as db:

        if args.mode == "predefined":
            if not args.script:
                return {"status": "error", "data": {"message": "Для mode=predefined укажите --script"}}
            return await predefined_mode.run(args.script, db, params=args.params,
                                              index_dir=get_vector_index_path())

        if not args.query:
            return {"status": "error", "data": {"message": f"Для mode={args.mode} требуется --query"}}

        if args.mode == "sql":
            return await sql_mode.run(args.query, db, context=context)

    if args.mode == "vector":
        index_dir = args.vector_index or get_vector_index_path()
        index_name = args.index_name or "audits_index"
        return await vector_mode.run(
            args.query, index_name, index_path=index_dir,
            top_k=args.top_k or 5,
            threshold=args.threshold,
        )

    return {"status": "error", "data": {"message": f"Неизвестный режим: {args.mode}"}}


def main() -> None:
    """
    Точка входа скрипта (вызывается из audit_analyze.bat/.sh).

    Pipeline:
        1. Парсинг аргументов (argparse)
        2. Запуск асинхронного _run() с маршрутизацией
        3. Форматирование результата (prepare_output)
        4. Вывод JSON в stdout

    Выходной JSON всегда содержит:
        - mode: режим работы
        - status: "success" | "error"
        - поля в зависимости от режима

    Пример вызова:
        python scripts/cli.py --mode predefined --script analytics_by_year_month \\
            --params '{"year": 2024}'
    """
    parser = _build_parser()
    args = parser.parse_args()

    result = asyncio.run(_run(args))
    output = _sanitize_value(prepare_output(result, args.mode))
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
