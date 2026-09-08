"""
Точка входа: CLI с разбором аргументов и маршрутизацией по режимам.

CLI — единая точка вызова навыка из shell/runtime/тестов. Он НЕ содержит
business-логики режимов: делегирует в ``predefined.run``, ``generated_sql_mode.run``,
``VectorSearchTool``. Какой mode выбрать — решает Agent/user; CLI лишь исполняет
запрошенную capability и сериализует результат в плоский JSON.

Modes:
    predefined     — выполнение готовых SQL-шаблонов (--script + --params)
    generated_sql  — генерация SQL через LLM по текстовому запросу (--query)
    vector         — семантический поиск по FAISS-индексу (--query + --index-name,
                     --top-k/--threshold)

Примеры запуска:
    # Предопределённый скрипт
    python scripts/cli.py --mode predefined \\
        --script violations_by_period \\
        --params '{"date_from": "2024-01-01", "date_to": "2024-12-31"}'

    # NL → SQL через LLM (требует LLM-ключ)
    python scripts/cli.py --mode generated_sql \\
        --query 'сколько аудитов было в 2024 по месяцам'

    # Векторный поиск (требует настроенный FAISS-индекс)
    python scripts/cli.py --mode vector \\
        --query 'пожарная безопасность' \\
        --index-name audits_index --top-k 5

    # С контекстом чата (история для LLM в generated_sql-режиме)
    python scripts/cli.py --mode generated_sql --query 'покажи детали' \\
        --context '[{"role":"user","content":"привет"}]'

Из output идёт JSON в stdout с плоской структурой
(см. ``output.prepare_output``).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any


# Подключаем scripts/ и корень проекта, чтобы sibling-модули импортировались.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
_PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
for p in (_PROJECT_ROOT, _SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from output import prepare_output, sanitize_output  # noqa: E402
from skill_config import (  # noqa: E402
    build_cache_provider,
    get_cli_config,
    get_in_memory_cache_path,
)
from workspace.skills.audit_analyzer.predefined import run as predefined_run  # noqa: E402

# IndexIntegrityError — generic core exception для STALE/INVALID FAISS.
from lib.services.cache_provider import IndexIntegrityError  # noqa: E402

MODES = ("predefined", "generated_sql", "vector")


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


def _ensure_registered() -> None:
    """Standalone-CLI регистрирует skill в ``TableRegistry`` перед работой.

    В обычном runtime это делает ``ApplicationContext`` (gateway). Для
    standalone-CLI без gateway — поднимаем самостоятельно, чтобы
    ``get_predefined_scripts_table()`` и ``search_vector`` находили
    таблицу/индекс. Идемпотентно: повторная регистрация игнорируется.
    """
    try:
        from config import SETTINGS
        from lib.core.infra_registration import register_vector_storage
        from lib.core.skill_registration import (
            register_embedding_config,
            register_skill_from_config,
        )

        audit_cfg = SETTINGS.get("skills", {}).get("audit_analyzer", {})
        register_skill_from_config("audit_analyzer", audit_cfg)
        register_vector_storage()
        register_embedding_config()
    except Exception as exc:
        print(f"[registration] WARN: {exc}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    """Argparse: --mode, --script, --query, --params, --index-name,
    --top-k, --threshold, --context."""
    default_mode = get_cli_config().get("default_mode", "predefined")
    parser = argparse.ArgumentParser(
        prog="audit_analyzer_cli",
        description=(
            "audit_analyzer: predefined SQL, NL->SQL через LLM, "
            "или vector-поиск по DuckDB-кэшу."
        ),
    )
    parser.add_argument(
        "--mode",
        default=default_mode,
        choices=MODES,
        help=(
            f"Режим: predefined / generated_sql / vector "
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
        help="Запрос на естественном языке (для mode=generated_sql/vector). "
             "Например: 'сколько аудитов было в 2024'",
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
        help="Контекст чата (JSON-список сообщений) для --mode generated_sql. "
             "Пример: '[{\"role\":\"user\",\"content\":\"привет\"}]'",
    )
    return parser


def _open_db():
    """Открыть DuckDB-кэш через CacheProvider (создаёт если нет)."""
    provider = build_cache_provider()
    cache_path = get_in_memory_cache_path()
    if hasattr(provider, "open_cache"):
        if not provider.open_cache():
            raise FileNotFoundError(
                f"DuckDB-кеш не найден: {cache_path}. "
                "Кеш создаёт и обновляет gateway автоматически — "
                "запустите его (python gateway.py)."
            )
    print(f"[DB] DuckDB cache ({cache_path})", file=sys.stderr)
    return provider


def _run_predefined(script: str, db: Any, params: dict[str, Any] | None) -> dict:
    """Запустить predefined capability через ``predefined.run()``."""
    if not script:
        return {
            "status": "error",
            "data": {
                "message": "Для --mode predefined укажите --script",
            },
        }
    return predefined_run(script, db, params=params)


def _run_generated_sql(query: str, db: Any, context: list[dict] | None) -> dict:
    """Запустить generated_sql capability через ``generated_sql_mode.run()``."""
    if not query:
        return {
            "status": "error",
            "data": {
                "message": "Для --mode generated_sql требуется --query",
            },
        }
    # Локальный импорт: generated_sql_mode → llm → lib.services.llm_client.
    # CLI-тесты запускают subprocess с минимальным env; держим импорт lazy,
    # чтобы --help/predefined не зависели от LLM-зависимостей.
    from generated_sql_mode import run as generated_sql_run

    return generated_sql_run(query, db, context=context)


def _run_vector(
    query: str,
    db: Any,
    index_name: str | None,
    top_k: int | None,
    threshold: float | None,
) -> dict:
    """Запустить vector capability через ``CacheProvider.search_vector()``.

    CLI использует **прямой** CacheProvider API (generic core), а не
    ``vector_search`` Tool — Skill не должен зависеть от Tool-реализации
    (см. ``docs/skill-tool-architecture.md`` — граница Skill ↔ Tool).
    Tool — для Agent, CLI — для shell/runtime и юнит-тестов.
    """
    if not query:
        return {
            "status": "error",
            "data": {
                "message": "Для --mode vector требуется --query",
            },
        }
    try:
        results = db.search_vector(
            query,
            index_name=index_name or "audits_index",
            top_k=top_k or 5,
            threshold=threshold,
        )
    except IndexIntegrityError as exc:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"vector index '{exc.index_name}' is {exc.status}: "
                    f"{exc.reason}. Пересоберите индекс через "
                    "tools/build_vectors.py."
                ),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "data": {"message": f"Внутренняя ошибка vector-поиска: {exc}"},
        }

    if not results:
        return {
            "status": "success",
            "data": {"message": "Документы не найдены", "results": [], "count": 0},
        }

    payload: dict[str, Any] = {
        "results": [asdict(r) for r in results],
        "count": len(results),
    }
    # STALE/INVALID detection: SearchResult.signature_status заполняется
    # провайдером. Если первый результат имеет непустой статус ≠ CURRENT —
    # отдаём warning клиенту (см. SearchResult.signature_status в
    # lib/services/cache_provider.py).
    first = results[0]
    sig_status = getattr(first, "signature_status", "") or ""
    if sig_status and sig_status != "CURRENT":
        payload["index_warning"] = {
            "status": sig_status,
            "reason": getattr(first, "signature_reason", "") or "",
            "recommendation": "rebuild index via tools/build_vectors.py",
        }
    return {"status": "success", "data": payload}


def _run(args: argparse.Namespace) -> dict:
    """Маршрутизация выполнения по ``args.mode``."""
    db = _open_db()
    try:
        if args.mode == "predefined":
            return _run_predefined(args.script, db, args.params)
        if args.mode == "generated_sql":
            return _run_generated_sql(args.query, db, args.context)
        if args.mode == "vector":
            return _run_vector(
                args.query, db, args.index_name, args.top_k, args.threshold
            )
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
        _ensure_registered()
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
