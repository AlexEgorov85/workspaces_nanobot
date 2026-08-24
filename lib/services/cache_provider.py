"""
Универсальный интерфейс провайдера кэша данных (СУБД + векторные индексы).

Слой абстрагирует два источника данных, типичных для RAG/аналитики:

  * **SQL-кэш** — локальная аналитическая БД (по умолчанию DuckDB-файл),
    которую можно создать/обновить из канонической PostgreSQL и запрашивать
    через query_sql / get_schema.
  * **Векторные индексы** — для семантического поиска (FAISS и т.п.):
    прогрев в память (preload_indexes) и поиск (search_vector).

Конкретная реализация не завязана на предметную область — любое приложение
(навык, модуль) получает эти методы через интерфейс и само интерпретирует
результаты. Сама реализация живёт в `cache_provider_impl.py` и управляется
из gateway (жизненный цикл: refresh / check_stale / preload_indexes / close).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchResult:
    """Один группированный результат векторного поиска по СУБД.

    Отражает запись в индексном источнике: исходный текст (content),
    метрику схожести (score) и атрибуты исходной строки (source/table/pk,
    полный row — исходная запись таблицы).
    """

    content: str
    score: float
    source: str = ""
    table: str = ""
    pk_value: Any = None
    chunk: str = ""
    matched_chunks: int = 1
    row: dict[str, Any] = field(default_factory=dict)


class CacheProvider(ABC):
    """Абстрактный провайдер кэша данных (SQL-кеш + векторные индексы)."""

    # -- lifecycle ------------------------------------------------------

    @abstractmethod
    def is_ready(self) -> bool:
        """Готов ли кэш к запросам (файл открыт и в памяти)."""
        raise NotImplementedError

    @abstractmethod
    def refresh(self) -> bool:
        """Создать/обновить SQL-кэш из канонической БД. True при успехе."""
        raise NotImplementedError

    @abstractmethod
    def check_stale(self) -> dict[str, Any]:
        """Сверить метки изменений (MAX updated) у таблиц кэша с источником.

        Возвращает dict с ключами: fresh, stale_tables, cache_meta, pg_meta.
        """
        raise NotImplementedError

    @abstractmethod
    def preload_indexes(self) -> list[dict[str, Any]]:
        """Прогреть векторные индексы из БД/файлов в память.

        Returns:
            Список загруженных индексов [{"index_name", "vectors"}, ...].
        """
        raise NotImplementedError

    # -- query ----------------------------------------------------------

    @abstractmethod
    def search_vector(
        self,
        query: str,
        index_name: str = "default_index",
        index_path: str | None = None,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResult]:
        """Семантический поиск по векторному индексу.

        Возвращает пустой список, если ничего не найдено или поиск
        невозможно выполнить (нет эмбеддинга / индекса).
        """
        raise NotImplementedError

    @abstractmethod
    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]:
        """Выполнить SELECT-запрос к SQL-кэшу (агрегации/отчёты).

        Returns:
            dict: {status, row_count, columns, rows} (+ error при ошибке).
        """
        raise NotImplementedError

    @abstractmethod
    def explain(self, sql: str) -> dict[str, Any]:
        """EXPLAIN на SQL-кэше — синтаксическая проверка без выполнения.

        Returns:
            dict: {"valid": True, "plan": [...]} или {"valid": False, "error": "..."}.
        """
        raise NotImplementedError

    @abstractmethod
    def get_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Получить структуру таблиц кэша (information_schema)."""
        raise NotImplementedError

    # -- resource -------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Закрыть открытые ресурсы (соединение кэша и т.п.)."""
        raise NotImplementedError
