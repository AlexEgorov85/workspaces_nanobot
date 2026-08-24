"""Pluggable table registry для синхронизации PostgreSQL → DuckDB.

Skills регистрируют свои таблицы и векторы через ``table_registry.register(...)``.
Core-инфраструктура (``ApplicationContext``, ``AuditSyncService``, ``CacheProvider``)
собирает все зарегистрированные сущности и создаёт единый runtime-снапшот.

Преимущества перед хардкод-конфигурацией (``audit_vector_settings``):

* новый skill добавляется без правок ``lib/`` — только ``register()`` в своём startup'е;
* core не знает имён конкретных навыков (TARGET §4, §22.9);
* один snapshot для всех навыков (``workspace/data_store/duckdb/cache.duckdb``)
  — все запросы через ``duckdb_query`` tool видят таблицы любого зарегистрированного skill'а.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SkillRegistration:
    """Описание таблиц и метаданных одного skill'а."""

    name: str
    tables: tuple[str, ...] = ()
    additional_tables: tuple[str, ...] = ()
    vector_table: str = ""
    db_schema: str = "main"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SkillRegistration.name is required")


@dataclass
class TableRegistry:
    """Singleton-реестр зарегистрированных skill'ов.

    Использование::

        from lib.services.table_registry import table_registry

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            tables=("oarb.audits", "oarb.violations"),
            additional_tables=("public.agent_predefined_scripts",),
            vector_table="oarb.audit_vectors",
            db_schema="oarb",
        ))

    Запрос всех зарегистрированных таблиц::

        all_tables = table_registry.all_tables()  # ("oarb.audits", "oarb.violations", ...)
        vector_table = table_registry.vector_table()  # "oarb.audit_vectors"

    Snapshot path — общий для всех skill'ов::

        path = table_registry.snapshot_path(workspace_path)
    """

    _registrations: dict[str, SkillRegistration] = field(default_factory=dict)
    _embedding: dict[str, Any] = field(default_factory=dict)

    def register(self, registration: SkillRegistration) -> None:
        """Зарегистрировать skill.

        Повторная регистрация того же ``name`` заменяет старую запись
        (полезно для тестов и hot-reload).
        """
        if not isinstance(registration, SkillRegistration):
            raise TypeError(
                f"expected SkillRegistration, got {type(registration).__name__}"
            )
        self._registrations[registration.name] = registration

    def unregister(self, name: str) -> None:
        """Удалить регистрацию skill'а (для graceful shutdown)."""
        self._registrations.pop(name, None)

    def get(self, name: str) -> Optional[SkillRegistration]:
        return self._registrations.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations.keys()))

    def all_tables(self) -> tuple[str, ...]:
        """Все таблицы для синхронизации PG → DuckDB (включая additional и vector)."""
        out: list[str] = []
        for reg in self._registrations.values():
            out.extend(reg.tables)
            out.extend(reg.additional_tables)
            if reg.vector_table and reg.vector_table not in out:
                out.append(reg.vector_table)
        return tuple(out)

    def store_tables(self) -> tuple[str, ...]:
        """Таблицы для in-memory store (без vector_table, чтобы publish не дублировал)."""
        out: list[str] = []
        for reg in self._registrations.values():
            out.extend(reg.tables)
            out.extend(reg.additional_tables)
        return tuple(out)

    def sync_tables(self) -> tuple[str, ...]:
        """Таблицы для синка (все, включая vector_table)."""
        return self.all_tables()

    def vector_table(self) -> Optional[str]:
        """Первая зарегистрированная vector_table (для FAISS)."""
        for reg in self._registrations.values():
            if reg.vector_table:
                return reg.vector_table
        return None

    def vector_db_tables(self) -> tuple[str, ...]:
        """Все vector_tables по навыкам (для случая, когда у каждого свой)."""
        out: list[str] = []
        for reg in self._registrations.values():
            if reg.vector_table:
                out.append(reg.vector_table)
        return tuple(out)

    def all_db_schemas(self) -> tuple[str, ...]:
        """Все схемы (для setup'а in-memory store)."""
        return tuple(sorted({reg.db_schema for reg in self._registrations.values()}))

    def clear(self) -> None:
        """Очистить реестр (для тестов)."""
        self._registrations.clear()

    def snapshot_path(self, workspace_path: Path, filename: str = "cache.duckdb") -> Path:
        """Путь к runtime-снапшоту DuckDB.

        По умолчанию: ``<workspace>/data_store/duckdb/cache.duckdb`` — единый
        файл для всех навыков. Это даёт cross-skill запросы через ``duckdb_query``.
        """
        return workspace_path / "data_store" / "duckdb" / filename

    def set_embedding_config(self, **kwargs: Any) -> None:
        """Установить embedding-конфиг, общий для всех навыков.

        Generic-инфраструктура: ``lib/`` не знает, какой именно skill
        регистрирует конфиг. Skill вызывает ``table_registry.set_embedding_config(...)``
        при старте (например, из своего startup-модуля).

        Поддерживаемые ключи: ``base_url``, ``model``, ``timeout_sec``,
        ``max_retries``, ``dimension``.
        """
        merged = dict(self._embedding)
        merged.update(kwargs)
        self._embedding = merged

    def embedding_config(self) -> dict[str, Any]:
        """Текущий embedding-конфиг (пустой dict, если не настроен)."""
        return dict(self._embedding)


# Глобальный singleton — skill'ы регистрируются в нём при старте.
table_registry = TableRegistry()


__all__ = [
    "SkillRegistration",
    "TableRegistry",
    "table_registry",
]