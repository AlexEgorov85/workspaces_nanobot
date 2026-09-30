"""Конфигурация эмбеддингов и объявление векторных индексов.

Портировано из агента: ``lib/services/cache_provider_impl.py``
(``read_embedding_config``, ``read_embedding_defaults``,
``read_vector_index_config``). Удаление агентской копии — фазы 4/5/9.

**Единственное отличие от агента — источник конфигурации.** Агентские функции
читали глобальный ``config.SETTINGS`` (то есть ``project.json`` агента) молча,
без параметра. В платформе ``project.json`` агента недоступен и запрещён к
импорту, поэтому конфигурация **передаётся параметром** — вызывающая сторона
(``server.py``) читает окружение и передаёт mapping дальше. Форма ключей и
значения по умолчанию сохранены, поэтому поведение build- и verify-сторон
остаётся тем же.

**Адреса и модели провайдера здесь больше нет.** В агентской копии они были
захардкожены (``EMBED_BASE_URL``/``EMBED_MODEL``) и жили рядом с владельцем
индекса. Это вторая копия конфигурации того же провайдера, которым владеет
``libs/llm``: она молча разошлась бы с настоящей при первой же правке модели.
Адрес и ключ читает ``libs/llm``; здесь остаются только параметры **индекса**
(модель и размерность эмбеддингов, влияющие на подпись индекса, и
chunk-параметры сборки) — их объявляет сервер из окружения.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Дефолтные chunk-параметры сборки индекса (используются, когда в объявлении
# индекса не заданы chunk_size / chunk_overlap). Это свойство сборки, а не
# конфигурация провайдера, поэтому дефолт здесь уместен.
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 80


def read_embedding_config(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Параметры эмбеддинга, влияющие на индекс: только то, что объявлено.

    Единая точка чтения для build- и verify-сторон, чтобы они не разошлись.
    Ключи: ``model``, ``dimension``, ``timeout_sec``.

    Незаданное значение остаётся ``None``, а не подставляется дефолтом: подпись
    индекса должна отражать конфигурацию процесса, и выдуманный дефолт сделал
    бы её одинаковой для настроенного и ненастроенного провайдера. Адрес и ключ
    здесь не читаются принципиально — это зона ``libs/llm``.
    """
    overrides = overrides or {}
    return {
        "model": overrides.get("model"),
        "dimension": overrides.get("dimension"),
        "timeout_sec": overrides.get("timeout_sec"),
    }


def read_embedding_defaults() -> dict[str, Any]:
    """Дефолтные chunk-параметры сборки (``DEFAULT_CHUNK_*``).

    Единая точка чтения: когда в объявлении индекса нет per-index
    chunk-параметров, и сборщик, и проверка подписи берут эти значения.
    """
    return {
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
    }


def read_vector_index_config(
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Объявления векторных индексов из конфигурации платформы.

    Аргумент — mapping той же формы, что ``project.json`` агента; функция
    разворачивает ``gateway.vector.index.indexes`` в pythonic-формат:
    ``{имя: {table, pk, source_table, content_columns, embedding_columns,
    track_column, chunk_size, chunk_overlap, metric, enabled}}``.

    Пустая/невнятная секция даёт ``{}`` — «индексов не объявлено» легитимно
    (индексы могут прийти из реестра снимка), и это НЕ ошибка.
    """
    vector = ((config.get("gateway") or {}).get("vector") or {})
    idx = vector.get("index") or {}
    indexes = idx.get("indexes") or {}
    if not isinstance(indexes, dict):
        return {}
    result: dict[str, Any] = {}
    for name, c in indexes.items():
        if not isinstance(c, dict):
            continue
        result[name] = {
            "table": c.get("table", ""),
            "pk": c.get("pk", ""),
            "source_table": c.get("source_table"),
            "content_columns": list(c.get("content_columns") or []),
            "embedding_columns": c.get("embedding_columns") or [],
            "track_column": c.get("track_column"),
            "chunk_size": c.get("chunk_size"),
            "chunk_overlap": c.get("chunk_overlap"),
            "metric": c.get("metric"),
            "enabled": c.get("enabled", True),
        }
    return result


def read_vector_storage_table(config: Mapping[str, Any]) -> str:
    """Имя таблицы векторного хранилища (``schema.table``) из конфигурации."""
    vector = ((config.get("gateway") or {}).get("vector") or {})
    idx = vector.get("index") or {}
    return str(idx.get("storage_table") or "")
