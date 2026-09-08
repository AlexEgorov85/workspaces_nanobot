"""Точка входа: CLI с разбором аргументов и маршрутизацией по режимам.

Режимы:
    predefined    — выполнение готовых SQL-шаблонов (--script + --params)
    generated_sql — LLM-генерация SELECT по текстовому запросу (--query)
    vector        — семантический поиск по FAISS-индексу (--query + --index-name,
                    --top-k/--threshold)

Примеры запуска:
    # Предопределённый скрипт
    python scripts/cli.py --mode predefined \\
        --script violations_by_period \\
        --params '{"date_from": "2024-01-01", "date_to": "2024-12-31"}'

    # SQL-генерация (требует LLM-ключ)
    python scripts/cli.py --mode generated_sql \\
        --query 'сколько аудитов было в 2024 по месяцам'

    # Векторный поиск (требует настроенный FAISS-индекс)
    python scripts/cli.py --mode vector \\
        --query 'пожарная безопасность' \\
        --index-name audits_index --top-k 5

Из output идёт JSON в stdout с плоской структурой
(см. ``output.prepare_output``).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Подключаем scripts/ и корень проекта, чтобы sibling-модули импортировались.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
for p in (_PROJECT_ROOT, _SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import generated_sql_mode  # noqa: E402
import predefined_mode  # noqa: E402
from output import prepare_output, sanitize_output  # noqa: E402
from skill_config import (  # noqa: E402
    build_cache_provider,
    get_cli_config,
    get_in_memory_config,
    get_vector_index_path,
)


def _parse_params(raw: str) -> dict[str, Any]:
    """Распарсить ``--params`` в dict. Поддерживает JSON и key=value."""
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


def _build_parser() -> argparse.ArgumentParser:
    """Argparse: --mode, --script, --query, --params, --vector-index,
    --index-name, --top-k, --threshold, --context."""
    default_mode = get_cli_config().get("default_mode", "predefined")
    parser = argparse.ArgumentParser(
        prog="audit_analyzer_cli",
        description=(
            "audit_analyzer: predefined SQL, LLM-генерация SELECT или "
            "vector-поиск по DuckDB-кэшу."
        ),
    )
    parser.add_argument(
        "--mode",
        default=default_mode,
        choices=["predefined", "generated_sql", "vector"],
        help=(
            f"Режим: predefined, generated_sql или vector "
            f"(default: {default_mode})"
        ),
    )
    parser.add_argument(
        "--script",
        default=None,
        help="Имя predefined-скрипта (для --mode predefined). "
             "Например: audit_status_summary, violations_by_period",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Запрос на естественном языке (для mode=generated_sql/vector).",
    )
    parser.add_argument(
        "--params",
        default=None,
        type=_parse_params,
        help='Параметры для --mode predefined. '
             'JSON: \'{"date_from": "2024-01-01"}\' '
             'или key=value: date_from=2024-01-01,date_to=2024-12-31',
    )
    parser.add_argument(
        "--vector-index",
        default=None,
        help="Каталог FAISS-индексов (для --mode vector).",
    )
    parser.add_argument(
        "--index-name",
        default=None,
        help="Имя индекса для --mode vector. По умолчанию: audits_index.",
    )
    parser.add_argument(
        "--top-k",
        default=None,
        type=int,
        help="Количество результатов для --mode vector (default: 5).",
    )
    parser.add_argument(
        "--threshold",
        default=None,
        type=float,
        help="Минимальный порог схожести 0.0–1.0 для --mode vector.",
    )
    parser.add_argument(
        "--context",
        default=None,
        type=json.loads,
        help='Контекст чата (JSON-список сообщений).',
    )
    return parser


def _open_db():
    """Открыть DuckDB-кэш через CacheProvider (создаёт если нет)."""
    im_cfg = get_in_memory_config()
    provider = build_cache_provider()
    cache_path = im_cfg.get("cache_path", "?")
    if hasattr(provider, "open_cache"):
        if not provider.open_cache():
            raise FileNotFoundError(
                f"DuckDB-кеш не найден: {cache_path}. "
                "Кеш создаёт и обновляет gateway автоматически — "
                "запустите его (python gateway.py)."
            )
    print(f"[DB] DuckDB cache ({cache_path})", file=sys.stderr)
    return provider


def _run(args: argparse.Namespace) -> dict:
    """Маршрутизация выполнения по ``args.mode``."""
    db = _open_db()
    try:
        if args.mode == "predefined":
            if not args.script:
                return {
                    "status": "error",
                    "data": {
                        "message": (
                            "Для --mode predefined укажите --script"
                        ),
                    },
                }
            return predefined_mode.run(
                args.script,
                db,
                params=args.params,
                index_dir=get_vector_index_path(),
            )

        if not args.query:
            return {
                "status": "error",
                "data": {
                    "message": f"Для --mode {args.mode} требуется --query",
                },
            }

        if args.mode == "generated_sql":
            return generated_sql_mode.run(
                args.query, db, context=args.context
            )

        if args.mode == "vector":
            from dataclasses import asdict

            results = db.search_vector(
                args.query,
                index_name=args.index_name or "audits_index",
                index_path=args.vector_index or get_vector_index_path(),
                top_k=args.top_k or 5,
                threshold=args.threshold,
            )
            if getattr(db, "_search_error", None):
                return {
                    "status": "error",
                    "data": {"message": db._search_error},
                }
            if not results:
                return {
                    "status": "success",
                    "data": {
                        "message": "Документы не найдены",
                        "results": [],
                        "count": 0,
                    },
                }
            return {
                "status": "success",
                "data": {
                    "results": [asdict(r) for r in results],
                    "count": len(results),
                },
            }

        return {
            "status": "error",
            "data": {"message": f"Неизвестный режим: {args.mode}"},
        }
    finally:
        if hasattr(db, "close"):
            db.close()


def main() -> None:
    """Entry point: argparse → _run → JSON в stdout."""
    try:
        parser = _build_parser()
        args = parser.parse_args()

        result = _run(args)
        out = sanitize_output(prepare_output(result, args.mode))
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    except argparse.ArgumentTypeError as e:
        print(json.dumps(
            {"mode": "unknown", "status": "error", "message": str(e)},
            ensure_ascii=False, indent=2,
        ))
        sys.exit(2)
    except FileNotFoundError as e:
        print(json.dumps(
            {"mode": "unknown", "status": "error", "message": str(e)},
            ensure_ascii=False, indent=2,
        ))
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps(
            {
                "mode": "unknown",
                "status": "error",
                "message": f"Внутренняя ошибка: {e}",
                "traceback": traceback.format_exc(),
            },
            ensure_ascii=False, indent=2,
        ))
        sys.exit(1)


if __name__ == "__main__":
    main()
