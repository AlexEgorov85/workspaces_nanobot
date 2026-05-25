# -*- coding: utf-8 -*-
"""Точка входа: CLI с аргументами, routing по режимам.

Поддерживает передачу контекста чата для интеграции с LLM агента.
"""

import argparse
import asyncio
import json
from typing import Optional, List

from .config import get_vector_index_path, load_db_config
from .output import prepare_output, serialize
from . import predefined_mode, sql_mode, vector_mode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="db_analyzer — анализ БД через LLM агента")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["predefined", "sql", "vector"],
        help="Режим работы",
    )
    parser.add_argument(
        "--script",
        default=None,
        help="Имя скрипта (обязательно для mode=predefined)",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Запрос на естественном языке",
    )
    parser.add_argument(
        "--vector-index",
        default=None,
        help="Путь к FAISS-индексу (только для vector mode, переопределяет VECTOR_INDEX_PATH)",
    )
    parser.add_argument(
        "--db-schema",
        default="oarb",
        help="Схема БД (по умолчанию oarb)",
    )
    parser.add_argument(
        "--context",
        default=None,
        help='Контекст чата в формате JSON (опционально, например: \'[{"role": "user", "content": "..."}]\")',
    )
    return parser


def _parse_context(context_str: Optional[str]) -> Optional[List[dict]]:
    """Парсинг контекста из JSON-строки."""
    if not context_str:
        return None
    try:
        return json.loads(context_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Некорректный JSON в --context: {e}")


async def _run(args: argparse.Namespace) -> dict:
    db_cfg = load_db_config()
    context = _parse_context(args.context)

    if args.mode == "predefined":
        if not args.script:
            return {"status": "error", "data": {"message": "Для mode=predefined укажите --script"}}
        return await predefined_mode.run(args.script, db_cfg)
    elif args.mode == "sql":
        return await sql_mode.run(args.query, db_cfg, context=context)
    elif args.mode == "vector":
        index_path = args.vector_index or get_vector_index_path()
        return await vector_mode.run(args.query, index_path, context=context)

    return {"status": "error", "data": {"message": f"Неизвестный режим: {args.mode}"}}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    result = asyncio.run(_run(args))
    output = prepare_output(result, args.mode)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=serialize))


if __name__ == "__main__":
    main()
