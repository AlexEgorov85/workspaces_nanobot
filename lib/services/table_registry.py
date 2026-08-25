"""Pluggable table registry для синхронизации PostgreSQL → DuckDB.

Skills регистрируют свои ресурсы (таблицы и векторы) через
``table_registry.register(...)``. Core-инфраструктура
(``ApplicationContext``, ``PgDuckDbSyncService``, ``CacheProvider``)
собирает все зарегистрированные ресурсы и создаёт единый runtime-снапшот.

Преимущества:

* новый skill добавляется без правок ``lib/`` — только ``register()`` в своём startup'е;
* core не знает имён конкретных навыков (TARGET §4, §22.9);
* один snapshot для всех навыков (``workspace/data_store/duckdb/cache.duckdb``)
  — все запросы через ``duckdb_query`` tool видят таблицы любого зарегистрированного skill'а.

Архитектурный контракт ресурсов:

* ``TableResource`` — описание одной PG-таблицы, которую skill хочет видеть
  в DuckDB-кэше. Знает имя, опциональную tracking-колонку и опциональный
  ``label`` (opaque-метка; если задана, таблица исключается из описания
  схемы для LLM, см. ``skill_config.get_db_tables()``).
* ``VectorResource`` — описание одной PG-таблицы сырых эмбеддингов,
  поверх которой строится FAISS-индекс.
* ``SkillRegistration.resources`` — единый набор ресурсов skill'а
  (таблицы + векторы). Это единственный источник истины о ресурсах skill'а.

Label — opaque marker для Skill-кода (например, ``"scripts_registry"`` для
реестра предопределённых SQL-скриптов в ``audit_analyzer``). Skill читает
таблицы с нужным label через ``TableRegistry.resources_by_label()``.
Runtime-инфраструктура (``lib/``) не интерпретирует значение label.

Никаких legacy-полей ``tables``/``additional_tables``/``vector_table``/
``track_column``/``track_column_overrides`` нет — старый код был мигрирован
на единый ``resources``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TableResource:
    """Декларативное описание одной PostgreSQL-таблицы, нужной skill'у.

    Ресурс только объявляет, ЧТО требуется skill'у. Он:

    * не открывает соединения;
    * не выполняет SQL;
    * не знает о DuckDB;
    * не управляет кэшем.

    Синхронизация ресурса выполняется инфраструктурным слоем
    (``PgDuckDbSyncService`` + ``DuckDbCacheStore``). По сути это DTO —
    единственный источник истины о том, какие таблицы skill хочет видеть.

    Архитектурный смысл: tracking-колонка — свойство конкретной таблицы,
    а не навыка в целом. Раньше tracking описывался через разрозненные
    поля skill'а (``track_column`` + ``track_column_overrides``); теперь
    это явный атрибут ресурса.

    Attributes:
        name: полное имя таблицы в формате ``schema.table``.
        tracking_column: колонка для инкрементального отслеживания изменений
            (обычно ``updated_at``). Если ``None``, sync-слой использует
            дефолт ``updated_at``.
        label: опциональная opaque-метка. Если задана, таблица исключается
            из описания схемы для LLM (см. ``skill_config.get_db_tables()``)
            и доступна только через
            ``TableRegistry.resources_by_label()``. Runtime-sync игнорирует.
    """

    name: str
    tracking_column: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.name or "." not in self.name:
            raise ValueError(
                f"TableResource.name должен быть в формате 'schema.table', "
                f"получено: {self.name!r}"
            )


@dataclass(frozen=True)
class VectorResource:
    """Декларативное описание одной PG-таблицы сырых эмбеддингов.

    Архитектурно vector-таблица — это **отдельный вид ресурса**, потому что
    она попадает в два независимых pipeline'а:

    * обычный table-sync (PG → DuckDB), чтобы search_vector мог читать
      эмбеддинги из снимка без обращения к PostgreSQL;
    * vector-индексация (FAISS) поверх DuckDB-снимка.

    Сейчас FAISS хранится в ``public.agent_vector_index_store`` и его
    параметры (model/dimension) живут в embedding-конфиге — поэтому сам
    ресурс знает только имя таблицы. Никаких ``embedding_model`` /
    ``dimension`` в ресурс не выносим: это конфигурация инфраструктуры,
    а не декларация ресурса.

    Attributes:
        name: полное имя таблицы в формате ``schema.table``.
        tracking_column: по умолчанию ``id`` (строки не апдейтятся —
            вставляются и остаются, монотонный PK). Явное значение
            переопределяет дефолт.
    """

    name: str
    tracking_column: str | None = None

    def __post_init__(self) -> None:
        if not self.name or "." not in self.name:
            raise ValueError(
                f"VectorResource.name должен быть в формате 'schema.table', "
                f"получено: {self.name!r}"
            )


Resource = TableResource | VectorResource


@dataclass(frozen=True)
class SkillRegistration:
    """Описание ресурсов одного skill'а.

    Это единственный источник истины о том, что skill хочет видеть в
    DuckDB-кэше и какие vector-таблицы нужны для FAISS-поиска.

    Attributes:
        name: уникальное имя skill'а (используется для идентификации
            владельца ресурса в Registry).
        resources: единый набор ресурсов skill'а (TableResource/VectorResource).
        enabled: если False — ресурсы этого skill'а пропускаются при sync.
            Полезно для отключения skill'а без удаления регистрации.
    """

    name: str
    resources: tuple[Resource, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SkillRegistration.name is required")

    def table_resources(self) -> tuple[TableResource, ...]:
        """Все ``TableResource`` этого skill'а (из единого ``resources``)."""
        return tuple(r for r in self.resources if isinstance(r, TableResource))

    def vector_resources(self) -> tuple[VectorResource, ...]:
        """Все ``VectorResource`` этого skill'а (из единого ``resources``)."""
        return tuple(r for r in self.resources if isinstance(r, VectorResource))

    def tracking_column_for(self, table: str) -> str:
        """Вернуть track-колонку для конкретной таблицы.

        Логика:
          * если таблица объявлена как ``VectorResource`` без явного
            ``tracking_column`` — возвращаем ``id`` (строки не апдейтятся,
            PK монотонный);
          * если таблица объявлена как ``TableResource`` / ``VectorResource``
            с явным ``tracking_column`` — возвращаем его;
          * если таблица не зарегистрирована в этом skill'е — возвращаем
            ``updated_at`` как generic-дефолт.
        """
        for r in self.resources:
            if isinstance(r, (TableResource, VectorResource)) and r.name == table:
                if r.tracking_column:
                    return r.tracking_column
                if isinstance(r, VectorResource):
                    return "id"
        return "updated_at"


@dataclass
class TableRegistry:
    """Singleton-реестр зарегистрированных skill'ов.

    Использование::

        from lib.services.table_registry import table_registry

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.violations"),
                TableResource(name="public.agent_predefined_scripts", label="scripts_registry"),
                VectorResource(name="oarb.audit_vectors"),
            ),
        ))

    Запрос всех зарегистрированных ресурсов::

        tables = table_registry.table_names()
        vectors = table_registry.vector_names()

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

    def get(self, name: str) -> SkillRegistration | None:
        return self._registrations.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations.keys()))

    def enabled_names(self) -> tuple[str, ...]:
        """Имена только enabled-registrations (для sync-планировщика)."""
        return tuple(
            sorted(name for name, reg in self._registrations.items() if reg.enabled)
        )

    def skill_for_table(self, table: str) -> SkillRegistration | None:
        """Найти регистрацию, владеющую данной таблицей (включая vector-таблицы)."""
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            all_names = [r.name for r in reg.table_resources()] + [
                r.name for r in reg.vector_resources()
            ]
            if table in all_names:
                return reg
        return None

    def resources_by_label(self, label: str) -> tuple[TableResource, ...]:
        """Все ``TableResource`` с заданным ``label`` (enabled-registrations only).

        Lookup-метод для skill-кода: позволяет skill'ам находить таблицы
        с определённой ролью (например, реестр предопределённых SQL-скриптов)
        без знания конкретных имён. Label — opaque marker; runtime-sync
        (``PgDuckDbSyncService``, ``DuckDbCacheStore``) его игнорирует.

        Disabled-регистрации пропускаются (соответствует семантике
        ``table_resources()`` и ``vector_resources()``).
        Дедупликация по ``name`` сохраняет порядок первой регистрации.
        """
        seen: dict[str, TableResource] = {}
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            for r in reg.table_resources():
                if r.label == label and r.name not in seen:
                    seen[r.name] = r
        return tuple(seen.values())

    def table_resources(self) -> tuple[TableResource, ...]:
        """Все ``TableResource`` всех enabled-registrations (для sync и cache)."""
        out: list[TableResource] = []
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            out.extend(reg.table_resources())
        return tuple(out)

    def vector_resources(self) -> tuple[VectorResource, ...]:
        """Все ``VectorResource`` всех enabled-registrations."""
        out: list[VectorResource] = []
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            out.extend(reg.vector_resources())
        return tuple(out)

    def resources(self) -> tuple[Resource, ...]:
        """Все ресурсы всех enabled-registrations (таблицы + векторы)."""
        return (*self.table_resources(), *self.vector_resources())

    def table_names(self) -> tuple[str, ...]:
        """Имена всех таблиц (TableResource) в порядке регистрации."""
        return tuple(dict.fromkeys(r.name for r in self.table_resources()))

    def vector_names(self) -> tuple[str, ...]:
        """Имена всех vector-таблиц (VectorResource) в порядке регистрации."""
        return tuple(dict.fromkeys(r.name for r in self.vector_resources()))

    def tracking_column_for(self, table: str) -> str:
        """Глобальный lookup: track-колонка для таблицы по любой регистрации."""
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            tc = reg.tracking_column_for(table)
            if table in self.table_names() or table in self.vector_names():
                return tc
        return "updated_at"

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
    "Resource",
    "SkillRegistration",
    "TableRegistry",
    "TableResource",
    "VectorResource",
    "table_registry",
]
