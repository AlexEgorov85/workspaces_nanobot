# -*- coding: utf-8 -*-
"""
audit_analyzer — анализ PostgreSQL-базы данных через LLM-агента.

Пакет предоставляет три режима работы, вызываемые через CLI:

    audit_analyze --mode <режим> [аргументы...]


Режимы
------

**predefined** — выполнение готовых SQL-шаблонов.

    audit_analyze --mode predefined \\
        --script analytics_by_year_month \\
        --params '{"year": 2024}'

    audit_analyze --mode predefined \\
        --script top_audited_objects \\
        --params '{"limit": 5}'

    audit_analyze --mode predefined \\
        --script violations_by_type \\
        --params '{"date_from": "2024-01-01", "violation_code": "финан"}'

**sql** — генерация и выполнение SELECT через LLM (любой OpenAI-compatible).

    audit_analyze --mode sql \\
        --query 'сколько аудитов было в 2024 году по каждому месяцу'

    audit_analyze --mode sql \\
        --query 'топ-10 объектов по количеству нарушений' \\
        --context '[{"role":"user","content":"привет"}]'

**vector** — семантический поиск по FAISS-индексу (Ollama embeddings).

    audit_analyze --mode vector \\
        --query 'нарушения пожарной безопасности' \\
        --index-name audits_index

    audit_analyze --mode vector \\
        --query 'финансовые несоответствия' \\
        --index-name violations_index \\
        --vector-index '/custom/path/to/index'


Аргументы CLI
-------------

--mode            Режим: predefined | sql | vector (обязательный)
--script          Имя скрипта (для predefined)
--query           Запрос на естественном языке (для sql/vector)
--params          Параметры скрипта в JSON (для predefined)
--vector-index    Директория с FAISS-индексами (для vector)
--index-name      Имя индекса без .faiss (для vector, по умолч. audits_index)
--context         История чата в JSON (для sql, опционально)


Структура пакета
----------------

scripts/
    __init__.py              — этот файл, публичный API
    cli.py                   — точка входа, парсинг аргументов, маршрутизация
    skill_config.py          — конфигурация навыка из SETTINGS, фабрика провайдера
    database.py              — PostgreSQL: схема, query, EXPLAIN (deprecated, in_memory_enabled=false)
    llm.py                   — LLM-клиент (OpenAI-compatible HTTP)
    output.py                — форматирование результата в JSON
    predefined.py            — обёртка над реестром скриптов (БД → DuckDB) + DynamicQueryBuilder
    predefined_mode.py       — оркестрация режима predefined
    scripts_registry.py      — ScriptDefinition, ParamDefinition, DynamicQueryBuilder
db_loader.py — адаптер: реестр из public.agent_predefined_scripts → ScriptDefinition
    sql_mode.py              — оркестрация режима sql (LLM + retry)

Инфраструктура (DuckDB-кэш, векторные индексы, эмбеддинг, чанкование
text_splitter.py) живёт в универсальном слое lib/services (cache_provider_impl.py)
и используется CLI навыка напрямую. Индексаторы — в tools/build_vectors.py.
"""
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Публичный API — функции верхнего уровня
# ---------------------------------------------------------------------------


def run_predefined(
    script_name: str,
    db_config: Optional[dict] = None,
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    try:
        import skill_config as _cfg
        from database import Database as _Db
        import predefined_mode as _pm
        if db_config is None:
            db_config = _cfg.load_db_config()
        with _Db(db_config) as db:
            return _pm.run(script_name, db, params=params,
                           index_dir=_cfg.get_vector_index_path())
    except Exception as e:
        return {"status": "error", "data": {"message": f"Ошибка в run_predefined: {e}"}}


def run_sql(
    query: str,
    db_config: Optional[dict] = None,
    context: Optional[List[dict]] = None,
) -> dict:
    try:
        import skill_config as _cfg
        from database import Database as _Db
        import sql_mode as _sm
        if db_config is None:
            db_config = _cfg.load_db_config()
        with _Db(db_config) as db:
            return _sm.run(query, db, context=context)
    except Exception as e:
        return {"status": "error", "data": {"message": f"Ошибка в run_sql: {e}"}}


def run_vector(
    query: str,
    index_name: str = "audits_index",
    index_path: Optional[str] = None,
    top_k: int = 5,
    threshold: Optional[float] = None,
) -> dict:
    try:
        from dataclasses import asdict
        import skill_config as _cfg
        if index_path is None:
            index_path = _cfg.get_vector_index_path()
        provider = _cfg.build_cache_provider()
        results = provider.search_vector(
            query, index_name=index_name, index_path=index_path,
            top_k=top_k, threshold=threshold,
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
    except Exception as e:
        return {"status": "error", "data": {"message": f"Ошибка в run_vector: {e}"}}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


def list_scripts() -> List[Dict[str, Any]]:
    import predefined as _pre
    return _pre.list_all_scripts()


def get_script(name: str) -> Optional[Dict[str, Any]]:
    import predefined as _pre
    sd = _pre.get_script_by_name(name)
    if sd is None:
        return None
    return {
        "name": sd.name,
        "description": sd.description,
        "parameters": {k: {"type": p.type, "required": p.required,
                           "default": p.default, "description": p.description}
                       for k, p in sd.parameters.items()},
        "returns": sd.returns,
        "long_description": sd.long_description,
        "max_rows_default": sd.max_rows_default,
    }


def load_config() -> dict:
    import skill_config as _cfg
    return _cfg.get_tool_config()


def refresh_config():
    import skill_config as _cfg
    _cfg.refresh_config()


# ---------------------------------------------------------------------------
# Версия
# ---------------------------------------------------------------------------

__version__ = "1.0.0"
__all__ = [
    "run_predefined",
    "run_sql",
    "run_vector",
    "list_scripts",
    "get_script",
    "load_config",
    "refresh_config",
]
