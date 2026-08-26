"""Pluggable реестр ресурсов runtime'а для синхронизации PostgreSQL → DuckDB.

Единая точка регистрации для двух видов ресурсов:

* skill-ресурсы (``SkillRegistration``): доменные таблицы и вектора,
  декларируются навыком в ``project.json::skills.<name>.tables[]``.
* инфраструктурные ресурсы (``register_infra``): общий runtime-storage,
  декларируется в ``gateway.vector.index.storage_table``.

Core-инфраструктура (``ApplicationContext``, ``PgDuckDbSyncService``,
``DuckDbCacheStore``) собирает ресурсы обоих видов через единые методы
(``table_names`` / ``vector_names`` / ``resources``) и создаёт общий
runtime-снапшот ``workspace/data_store/duckdb/cache.duckdb``.

Архитектурный контракт:

* ``TableResource`` — описание одной PG-таблицы для DuckDB-кэша. Опциональная
  ``label`` (opaque-метка) исключает таблицу из описания схемы для LLM
  и делает её доступной только через ``TableRegistry.resources_by_label()``.
  Runtime-sync ``label`` игнорирует.
* ``VectorResource`` — описание одной PG-таблицы сырых эмбеддингов. FAISS
  строится поверх неё (через ``public.agent_vector_index_config`` +
  ``agent_vector_index_store``), параметры model/dimension — в
  embedding-конфиге, не в ресурсе.
* ``SkillRegistration.resources`` — единый набор ресурсов skill'а.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TableResource:
    """Декларативное описание одной PG-таблицы для DuckDB-кэша.

    DTO: только имя, опциональная tracking-колонка, опциональная ``label``.
    Не открывает соединения, не выполняет SQL, не управляет кэшем —
    синхронизация выполняется инфраструктурным слоем
    (``PgDuckDbSyncService`` + ``DuckDbCacheStore``).

    Attributes:
        name: полное имя таблицы в формате ``schema.table``.
        tracking_column: колонка для инкрементального отслеживания изменений
            (обычно ``updated_at``). ``None`` → дефолт ``updated_at``.
        label: opaque-метка. Если задана, таблица исключается из описания
            схемы для LLM (см. ``skill_config.get_db_tables()``) и доступна
            только через ``TableRegistry.resources_by_label()``.
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

    Vector-таблица попадает в два независимых pipeline'а: обычный table-sync
    (PG → DuckDB) для чтения эмбеддингов через ``vector_search``, и
    vector-индексация (FAISS) поверх DuckDB-снимка. Параметры embedding
    (model/dimension) живут в embedding-конфиге, не здесь.

    Attributes:
        name: полное имя таблицы в формате ``schema.table``.
        tracking_column: дефолт ``id`` (строки не апдейтятся, PK монотонный).
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

    Attributes:
        name: уникальное имя skill'а.
        resources: единый набор ресурсов (TableResource/VectorResource).
        enabled: ``False`` — ресурсы пропускаются при sync.
    """

    name: str
    resources: tuple[Resource, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SkillRegistration.name is required")

    def table_resources(self) -> tuple[TableResource, ...]:
        """Все ``TableResource`` этого skill'а."""
        return tuple(r for r in self.resources if isinstance(r, TableResource))

    def vector_resources(self) -> tuple[VectorResource, ...]:
        """Все ``VectorResource`` этого skill'а."""
        return tuple(r for r in self.resources if isinstance(r, VectorResource))

    def tracking_column_for(self, table: str) -> str:
        """Track-колонка для таблицы в этом skill'е.

        ``VectorResource`` без явного ``tracking_column`` → ``"id"``
        (append-only, монотонный PK). Явный ``tracking_column`` имеет
        приоритет. Незарегистрированная таблица → ``"updated_at"``.
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
    """Singleton-реестр ресурсов runtime'а.

    Два независимых namespace'а:

    * ``_registrations`` — skill-ресурсы (доменные таблицы + вектора),
      ключ — ``SkillRegistration.name``. Регистрируются через
      ``register(SkillRegistration(...))``.
    * ``_infra`` — общие runtime-ресурсы (например, единый storage сырых
      эмбеддингов ``oarb.audit_vectors``). Ключ — логический
      идентификатор (``"vector_index.storage"``). Регистрируются через
      ``register_infra(key, resources)``.

    Методы-агрегаторы (``table_names``, ``vector_names``, ``resources``,
    ``tracking_column_for``) объединяют оба namespace'а — сборка
    runtime'а (``_make_sync_services``, ``_make_cache_store``) не различает
    источник ресурса.

    Attributes:
        _registrations: skill-регистрации.
        _infra: инфраструктурные ресурсы.
        _embedding: общий embedding-конфиг (base_url, model, dimension).
    """

    _registrations: dict[str, SkillRegistration] = field(default_factory=dict)
    _infra: dict[str, tuple[Resource, ...]] = field(default_factory=dict)
    _embedding: dict[str, Any] = field(default_factory=dict)

    def register(self, registration: SkillRegistration) -> None:
        """Зарегистрировать skill."""
        if not isinstance(registration, SkillRegistration):
            raise TypeError(
                f"expected SkillRegistration, got {type(registration).__name__}"
            )
        self._registrations[registration.name] = registration

    def register_infra(self, key: str, resources: tuple[Resource, ...]) -> None:
        """Зарегистрировать инфраструктурные ресурсы.

        ``key`` — логический идентификатор namespace'а. Не пересекается
        с именами skill'ов.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("register_infra.key должен быть непустой строкой")
        if not isinstance(resources, tuple):
            raise TypeError(
                f"register_infra.resources должен быть tuple, "
                f"got {type(resources).__name__}"
            )
        for r in resources:
            if not isinstance(r, (TableResource, VectorResource)):
                raise TypeError(
                    f"register_infra.resources: ожидается TableResource/VectorResource, "
                    f"got {type(r).__name__}"
                )
        self._infra[key] = resources

    def unregister_infra(self, key: str) -> None:
        """Удалить инфраструктурную регистрацию."""
        self._infra.pop(key, None)

    def unregister(self, name: str) -> None:
        """Удалить регистрацию skill'а."""
        self._registrations.pop(name, None)

    def get(self, name: str) -> SkillRegistration | None:
        return self._registrations.get(name)

    def get_infra(self, key: str) -> tuple[Resource, ...]:
        """Инфраструктурные ресурсы по ``key`` (пустой tuple, если нет)."""
        return self._infra.get(key, ())

    def infra_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._infra.keys()))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations.keys()))

    def enabled_names(self) -> tuple[str, ...]:
        """Имена enabled-registrations."""
        return tuple(
            sorted(name for name, reg in self._registrations.items() if reg.enabled)
        )

    def skill_for_table(self, table: str) -> SkillRegistration | None:
        """Найти регистрацию, владеющую таблицей."""
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
        """Все ``TableResource`` skill'ов с заданным ``label``.

        Ищет только в skill-регистрациях (label — доменная метка,
        инфра-ресурсы её не имеют). Disabled-регистрации пропускаются.
        Дедупликация по ``name``.
        """
        seen: dict[str, TableResource] = {}
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            for r in reg.table_resources():
                if r.label == label and r.name not in seen:
                    seen[r.name] = r
        return tuple(seen.values())

    def _infra_table_resources(self) -> tuple[TableResource, ...]:
        return tuple(r for r in self._iter_infra() if isinstance(r, TableResource))

    def _infra_vector_resources(self) -> tuple[VectorResource, ...]:
        return tuple(r for r in self._iter_infra() if isinstance(r, VectorResource))

    def _iter_infra(self) -> tuple[Resource, ...]:
        out: list[Resource] = []
        for resources in self._infra.values():
            out.extend(resources)
        return tuple(out)

    def table_resources(self) -> tuple[TableResource, ...]:
        """Все ``TableResource`` (skills + infra).

        Порядок: skill-ресурсы, затем инфра-ресурсы.
        """
        out: list[TableResource] = []
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            out.extend(reg.table_resources())
        out.extend(self._infra_table_resources())
        return tuple(out)

    def vector_resources(self) -> tuple[VectorResource, ...]:
        """Все ``VectorResource`` (skills + infra).

        Порядок: skill-ресурсы, затем инфра-ресурсы.
        """
        out: list[VectorResource] = []
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            out.extend(reg.vector_resources())
        out.extend(self._infra_vector_resources())
        return tuple(out)

    def resources(self) -> tuple[Resource, ...]:
        """Все ресурсы (skills + infra, таблицы + векторы)."""
        return (*self.table_resources(), *self.vector_resources())

    def table_names(self) -> tuple[str, ...]:
        """Имена всех таблиц в порядке регистрации."""
        return tuple(dict.fromkeys(r.name for r in self.table_resources()))

    def vector_names(self) -> tuple[str, ...]:
        """Имена всех vector-таблиц в порядке регистрации."""
        return tuple(dict.fromkeys(r.name for r in self.vector_resources()))

    def tracking_column_for(self, table: str) -> str:
        """Track-колонка для таблицы (skills + infra)."""
        for reg in self._registrations.values():
            if not reg.enabled:
                continue
            tc = reg.tracking_column_for(table)
            if table in self.table_names() or table in self.vector_names():
                return tc
        for r in self._iter_infra():
            if r.name == table:
                if getattr(r, "tracking_column", None):
                    return r.tracking_column
                if isinstance(r, VectorResource):
                    return "id"
        return "updated_at"

    def clear(self) -> None:
        """Очистить реестр."""
        self._registrations.clear()
        self._infra.clear()

    def snapshot_path(self, workspace_path: Path, filename: str = "cache.duckdb") -> Path:
        """Путь к runtime-снапшоту DuckDB.

        По умолчанию: ``<workspace>/data_store/duckdb/cache.duckdb`` — единый
        файл для всех навыков (cross-skill запросы через ``duckdb_query``).
        """
        return workspace_path / "data_store" / "duckdb" / filename

    def set_embedding_config(self, **kwargs: Any) -> None:
        """Установить embedding-конфиг (общий для всех навыков).

        Ключи: ``base_url``, ``model``, ``timeout_sec``, ``max_retries``,
        ``dimension``.
        """
        merged = dict(self._embedding)
        merged.update(kwargs)
        self._embedding = merged

    def embedding_config(self) -> dict[str, Any]:
        """Текущий embedding-конфиг (пустой dict, если не настроен)."""
        return dict(self._embedding)


# Глобальный singleton.
table_registry = TableRegistry()


__all__ = [
    "Resource",
    "SkillRegistration",
    "TableRegistry",
    "TableResource",
    "VectorResource",
    "table_registry",
]
