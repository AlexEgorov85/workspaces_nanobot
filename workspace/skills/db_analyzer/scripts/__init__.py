# -*- coding: utf-8 -*-
"""
db_analyzer — анализ PostgreSQL-базы данных через LLM-агента.

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

**sql** — генерация и выполнение SELECT через LLM (Mistral AI).

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
    config.py                — загрузка config.json, геттеры
    database.py              — PostgreSQL: схема, execute, EXPLAIN, валидация
    llm.py                   — LLM-клиент (OpenAI-compatible HTTP)
    output.py                — форматирование результата в JSON
    predefined.py            — обёртка над SCRIPTS_REGISTRY + DynamicQueryBuilder
    predefined_mode.py       — оркестрация режима predefined
    scripts_registry.py      — ScriptDefinition, ParamDefinition, DynamicQueryBuilder, реестр
    sql_mode.py              — оркестрация режима sql (LLM + retry)
    vector_mode.py           — оркестрация режима vector (FAISS + embeddings)

"""
from typing import Any, Dict, List, Optional, Tuple

import cli
import config
import database
from database import Database
import llm
import output
import predefined
import predefined_mode
import scripts_registry
import sql_mode
import vector_mode

# ---------------------------------------------------------------------------
# Публичный API — функции верхнего уровня
# ---------------------------------------------------------------------------


def run_predefined(
    script_name: str,
    db_config: Optional[dict] = None,
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Выполнить предопределённый SQL-скрипт.

    Args:
        script_name: Имя скрипта (ключ в SCRIPTS_REGISTRY).
        db_config: Конфиг БД (из config.load_db_config() если None).
        params: Параметры скрипта (опционально).

    Returns:
        dict с результатом: status, data (script_name, sql, parameters, result).
    """
    try:
        if db_config is None:
            db_config = config.load_db_config()
        with Database(db_config) as db:
            return predefined_mode.run(script_name, db, params=params,
                                        index_dir=config.get_vector_index_path())
    except Exception as e:
        return {"status": "error", "data": {"message": f"Ошибка в run_predefined: {e}"}}


def run_sql(
    query: str,
    db_config: Optional[dict] = None,
    context: Optional[List[dict]] = None,
) -> dict:
    """
    Сгенерировать и выполнить SQL-запрос через LLM.

    Args:
        query: Запрос на естественном языке.
        db_config: Конфиг БД (из config.load_db_config() если None).
        context: История чата (опционально).

    Returns:
        dict с результатом: status, data (sql, result).
    """
    try:
        if db_config is None:
            db_config = config.load_db_config()
        with Database(db_config) as db:
            return sql_mode.run(query, db, context=context)
    except Exception as e:
        return {"status": "error", "data": {"message": f"Ошибка в run_sql: {e}"}}


def run_vector(
    query: str,
    index_name: str = "audits_index",
    index_path: Optional[str] = None,
    top_k: int = 5,
    threshold: Optional[float] = None,
) -> dict:
    """
    Выполнить семантический поиск по FAISS-индексу.

    Args:
        query: Текстовый запрос.
        index_name: Имя индекса (без .faiss).
        index_path: Путь к директории с индексами (из конфига если None).
        top_k: Количество результатов (по умолч. 5, игнорируется при threshold).
        threshold: Порог схожести (0.0–1.0). Если задан, top_k игнорируется.

    Returns:
        dict с результатом: status, data (results, count).
    """
    try:
        if index_path is None:
            index_path = config.get_vector_index_path()
        return vector_mode.run(query, index_name, index_path=index_path,
                                top_k=top_k, threshold=threshold)
    except Exception as e:
        return {"status": "error", "data": {"message": f"Ошибка в run_vector: {e}"}}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


def list_scripts() -> List[Dict[str, Any]]:
    """
    Список всех предопределённых скриптов.

    Returns:
        Список dict: name, description, parameters.

    Пример:
        >>> list_scripts()
        [{'name': 'analytics_by_year_month', 'description': 'Аналитика...', 'parameters': ['year']}, ...]
    """
    return predefined.list_all_scripts()


def get_script(name: str) -> Optional[Dict[str, Any]]:
    """
    Информация о конкретном скрипте.

    Args:
        name: Имя скрипта.

    Returns:
        dict с полями ScriptDefinition или None.

    Пример:
        >>> s = get_script("violations_by_type")
        >>> s["description"]
        'Статистика нарушений по типам и категориям'
    """
    sd = predefined.get_script_by_name(name)
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
    """
    Полная конфигурация db_analyzer.

    Returns:
        dict с содержимым config.json.

    Пример:
        >>> cfg = load_config()
        >>> cfg["llm"]["model"]
        'mistral-large-latest'
    """
    return config.get_tool_config()


def refresh_config():
    """
    Принудительно перечитать config.json.

    Пример:
        >>> refresh_config()
    """
    config.refresh_config()


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
    "cli",
    "config",
    "database",
    "llm",
    "output",
    "predefined",
    "predefined_mode",
    "scripts_registry",
    "sql_mode",
    "vector_mode",
]
