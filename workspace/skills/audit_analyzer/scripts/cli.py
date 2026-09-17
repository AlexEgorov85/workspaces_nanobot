"""
Точка входа: CLI с разбором аргументов и маршрутизацией по режимам.

CLI — единая точка вызова навыка из shell/runtime/тестов. Он НЕ содержит
business-логики режимов: делегирует в ``predefined.run``, ``generated_sql_mode.run``,
``CacheProvider.search_vector``. Какой mode выбрать — решает Agent/user; CLI лишь исполняет
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
    get_predefined_scripts_table,
)
from workspace.skills.audit_analyzer.scripts.predefined import run as predefined_run  # noqa: E402

# IndexIntegrityError — generic core exception для STALE/INVALID FAISS.
from lib.services.cache_provider import IndexIntegrityError  # noqa: E402

MODES = ("predefined", "generated_sql", "vector")


def _resolve_known_index(index_name: str) -> tuple[bool | None, str]:
    """Проверить, что ``index_name`` зарегистрирован в runtime-реестре.

    Использует публичный ``cache_provider_impl.read_vector_index_config({})`` —
    не лезем в приватное состояние provider'а.

    Returns:
        ``(True, "")`` — индекс зарегистрирован.
        ``(False, "message with available names")`` — индекс не найден.
        ``(None, "registry-error message")`` — registry недоступен (PG
        недоступна или реестр пустой); вызывающий решит, отказывать ли.

    Для standalone-CLI реестр живёт в PostgreSQL — если gateway не
    запущен или PG недоступна, registry вернёт ошибку. В этом случае
    CLI **не должен** молча пропускать запрос (иначе silent fail на
    неизвестном индексе), а отдаёт error с подсказкой.
    """
    try:
        from lib.services.cache_provider_impl import read_vector_index_config
        names = sorted(read_vector_index_config({}).keys())
    except Exception as exc:
        return (
            None,
            f"Не удалось прочитать runtime-реестр индексов "
            f"(read_vector_index_config): {exc}. Запустите gateway "
            f"(python gateway.py) — реестр индексов живёт в PostgreSQL.",
        )
    if index_name in names:
        return (True, "")
    available = ", ".join(names) if names else "(реестр пуст)"
    return (
        False,
        f"vector index '{index_name}' is not registered. "
        f"Available indexes: {available}. Зарегистрируйте индекс "
        f"через tools/build_vectors.py.",
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


def _ensure_registered() -> None:
    """Standalone-CLI регистрирует skill в ``TableRegistry`` перед работой.

    В обычном runtime это делает ``ApplicationContext`` (gateway). Для
    standalone-CLI без gateway — поднимаем самостоятельно, чтобы
    ``get_predefined_scripts_table()`` и ``search_vector`` находили
    таблицу/индекс. Идемпотентно: повторная регистрация игнорируется.

    После change ``config-profile-cli-flag`` ``SETTINGS`` —
    ``_LazySettings`` proxy, который публикуется через
    ``config._initialize_settings(profile)``. В standalone-CLI нет
    никого, кто бы вызвал ``_initialize_settings``, поэтому делаем
    это здесь (default = test, fail-safe для ad-hoc запусков). Если
    proxy уже инициализирован entrypoint'ом (CLI запущен внутри
    gateway/cli_agent/streamlit) — этот вызов no-op (повторный init
    бросает ``already initialized``, который мы ловим).
    """
    try:
        import config as _cfg
        if not _cfg.is_settings_initialized():
            _cfg._initialize_settings(profile="test")
    except Exception:
        pass  # registration может продолжаться без настроенного профиля
    try:
        from config import SETTINGS
        from lib.core.infra_registration import register_vector_storage
        from lib.core.skill_registration import register_skill_from_config

        audit_cfg = SETTINGS.get("skills", {}).get("audit_analyzer", {})
        register_skill_from_config("audit_analyzer", audit_cfg)
        register_vector_storage()
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
        help="Имя predefined-скрипта (для --mode predefined).",
    )
    parser.add_argument(
        "--list-scripts",
        action="store_true",
        help="Для --mode predefined: вывести полный каталог "
             "predefined-скриптов из БД (name, description, parameters) и выйти.",
    )
    parser.add_argument(
        "--list-indexes",
        action="store_true",
        help="Для --mode vector: вывести каталог FAISS-индексов из БД "
             "(name, source_table, embed-колонки) и выйти.",
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
        help="Имя индекса для --mode vector.",
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
        help="Контекст чата (JSON-список сообщений) для --mode generated_sql.",
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


def _list_scripts(db: Any) -> dict:
    """Полный каталог predefined-скриптов из ``public.agent_predefined_scripts``.

    Включает name, description, parameters (JSONB-структура
    ParamDefinition: type/required/default/description), returns,
    long_description, max_rows_default, sql_template. Используется
    CLI-флагом ``--list-scripts`` для discovery без чтения ``SKILL.md``.
    """
    try:
        predefined_table = get_predefined_scripts_table()
    except Exception as exc:
        return {
            "status": "error",
            "data": {
                "message": f"Не удалось зарезолвить таблицу predefined-скриптов: {exc}",
                "error_type": "registry_unavailable",
            },
        }
    from workspace.skills.audit_analyzer.scripts.predefined import load_all

    scripts = load_all(db, predefined_table)
    items = []
    for name in sorted(scripts.keys()):
        s = scripts[name]
        items.append(
            {
                "name": s.name,
                "description": s.description,
                "long_description": s.long_description,
                "parameters": {
                    pname: {
                        "type": pdef.type,
                        "required": pdef.required,
                        "description": pdef.description,
                    }
                    for pname, pdef in s.parameters.items()
                },
                "max_rows_default": s.max_rows_default,
            }
        )
    return {
        "status": "success",
        "data": {
            "predefined_table": predefined_table,
            "count": len(items),
            "scripts": items,
        },
    }


def _list_indexes() -> dict:
    """Каталог runtime-индексов из DuckDB-снапшота ``<storage_table>``.

    После change ``remove-vector-index-store`` persisted FAISS-кеш
    (``agent_vector_index_store``) удалён. Runtime-состояние индексов
    — это набор ``source`` (index_name), присутствующих в DuckDB-кэше
    ``storage_table`` (синхронизируется через ``PgDuckDbSyncService``).
    Это фактические артефакты, которые runtime реально увидит при поиске.
    Конфиг декларации (``project.json::gateway.vector.index.indexes``)
    здесь **не** используется — он покажет то, что обещано построить,
    а не то, что реально собрано. Сравнить их двух — задача
    ``tools/check_indexes.py`` (MISSING/ORPHAN/STALE/INVALID).

    Поля элемента списка:
      ``index_name``        — имя индекса (= ``source`` в storage_table);
      ``vectors``           — количество чанков (DuckDB COUNT(*));
      ``dimension``         — ``None`` (DuckDB не хранит размерность
                              векторов как поле);
      ``signature_status``  — ``CURRENT`` если index_name в ``declared``,
                              иначе ``ORPHAN`` (нет persisted-signature —
                              не вычисляется без runtime-индекса);
      ``updated_at``        — ``None`` (нет persisted-метаданных).
    """
    try:
        from lib.services.cache_provider_impl import (
            list_runtime_vector_indexes,
            read_vector_index_config,
        )
    except Exception as exc:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Не удалось импортировать vector-discovery хелперы: {exc}."
                ),
                "error_type": "import_failed",
            },
        }

    try:
        runtime = list_runtime_vector_indexes()
    except Exception as exc:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"DuckDB-снапшот ``<storage_table>`` недоступен: {exc}. "
                    f"Это инфраструктурная ошибка (exit 2 в tools/check_indexes.py)."
                ),
                "error_type": "store_unavailable",
            },
        }

    declared = read_vector_index_config({}) or {}

    items: list[dict] = []
    for row in sorted(runtime, key=lambda r: r.get("source") or ""):
        name = row.get("source") or ""
        status = "CURRENT" if name in declared else "ORPHAN"
        items.append({
            "index_name": name,
            "vectors": row.get("vector_count"),
            "dimension": row.get("dimension"),
            "metric": row.get("metric"),
            "signature_status": status,
            "signature_short": None,
            "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
        })

    return {
        "status": "success",
        "data": {
            "count": len(items),
            "indexes": items,
            "note": (
                "Source: DuckDB-снапшот ``<storage_table>`` (runtime artifacts). "
                "Compare against ``project.json::gateway.vector.index.indexes`` "
                "via ``tools/check_indexes.py`` to see declared-but-missing indexes."
            ),
        },
    }


def _run_predefined(script: str, db: Any, params: dict[str, Any] | None) -> dict:
    """Запустить predefined capability через ``predefined.run()``.

    Скрипты читаются из PG-снимка ``public.agent_predefined_scripts``
    (см. ``sql/audit_analyzer/seed_predefined_scripts.sql``) — это
    канонический source of truth. ``TableRegistry`` ищет таблицу по
    label ``scripts_registry``; registration происходит в
    ``_ensure_registered()`` при старте CLI.
    """
    if not script:
        return {
            "status": "error",
            "data": {
                "message": "Для --mode predefined укажите --script",
            },
        }
    try:
        predefined_table = get_predefined_scripts_table()
    except Exception as exc:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"Не удалось зарезолвить таблицу predefined-скриптов: {exc}. "
                    "Проверьте, что skill audit_analyzer зарегистрирован "
                    "(project.json::skills.audit_analyzer.tables содержит "
                    "{\"name\": \"public.agent_predefined_scripts\", "
                    "\"label\": \"scripts_registry\"})."
                ),
                "error_type": "registry_unavailable",
            },
        }
    return predefined_run(script, db, params=params, predefined_table=predefined_table)


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

    # Валидация index_name ДО search_vector: иначе неизвестный индекс
    # тихо вернёт [] через _search_error в provider'е, и пользователь
    # увидит «Документы не найдены» как success (silent fail).
    target_index = index_name or "audits_index"
    known, registry_msg = _resolve_known_index(target_index)
    if known is False:
        return {
            "status": "error",
            "data": {"message": registry_msg, "error_type": "unknown_index"},
        }
    if known is None:
        # Registry недоступен (PG offline и т.п.). Не пускаем дальше —
        # иначе снова рискуем silent fail.
        return {
            "status": "error",
            "data": {
                "message": registry_msg,
                "error_type": "registry_unavailable",
            },
        }

    try:
        results = db.search_vector(
            query,
            index_name=target_index,
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
        if getattr(args, "list_scripts", False):
            return _list_scripts(db)
        if getattr(args, "list_indexes", False):
            return _list_indexes()
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
        # ``--list-scripts`` / ``--list-indexes`` не проходят через
        # ``prepare_output`` — там формат вывода другой
        # (каталог скриптов/индексов из БД, без rows/columns).
        if getattr(args, "list_scripts", False) or getattr(args, "list_indexes", False):
            out = result
        else:
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
