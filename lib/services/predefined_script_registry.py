"""Реестр предопределённых SQL-скриптов (``PredefinedScriptRegistry``).

Источник истины — таблица PG ``public.agent_predefined_scripts``
(DDL: ``sql/audit_analyzer/create_public_agent_predefined_scripts.sql``),
зарегистрированная в ``TableRegistry`` через
``label="scripts_registry"``. Через ``PgDuckDbSyncService`` данные
попадают в общий runtime-снапшот DuckDB, откуда реестр их и читает.

Этот модуль — **read-only resolution + metadata**:
  * ``get_by_id(name)`` / ``get_by_name(name)``
  * ``list_all()``
  * ``find(query, top_k)`` — keyword-overlap по ``name`` / ``description`` /
    ``long_description`` (без ts_rank, без embeddings — следуем паттерну
    ``NlSqlRunner._select_few_shot``).

**Не выполняет SQL.** Подготовка запроса (валидация параметров, resolve
placeholder'ов) — ответственность ``PredefinedScriptRequestBuilder``
(Этап 3). Выполнение — через **существующий** ``CacheProvider.query_sql``
после ``validate_sql`` (Этап 4).

Это generic (domain-free) сервис: не импортирует ``workspace.skills.*``
(TARGET §4, §22.8). Имя реестра (``label='scripts_registry'``) — единственная
конвенция, общая с ``NlSqlRunner``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


__all__ = [
    "PredefinedScript",
    "PredefinedScriptRegistry",
]


_TOKEN_SPLIT_RE = re.compile(r"[^a-zа-яё0-9]+")


class _ProviderProtocol(Protocol):
    """Минимальный интерфейс ``CacheProvider`` для реестра."""

    def query_sql(self, sql: str, params: list | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PredefinedScript:
    """Описание одного предопределённого скрипта.

    Атрибуты соответствуют колонкам ``public.agent_predefined_scripts``.
    ``parameters`` — распарсенный JSONB (``dict[str, dict[str, Any]]``).
    """

    name: str
    description: str
    sql_template: str
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_rows_default: int = 0
    returns: str = ""
    long_description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def parameter_names(self) -> tuple[str, ...]:
        return tuple(self.parameters.keys())


class PredefinedScriptRegistry:
    """Реестр предопределённых SQL-скриптов (read-only).

    Args:
        provider: ``CacheProvider`` для чтения реестра из DuckDB.
        label: метка ``TableResource.label`` (по умолчанию
            ``"scripts_registry"``).
    """

    _REGISTRY_COLUMNS = (
        "name, description, sql_template, parameters, "
        "max_rows_default, returns, long_description"
    )

    def __init__(
        self,
        *,
        provider: _ProviderProtocol,
        label: str = "scripts_registry",
    ) -> None:
        self._provider = provider
        self._label = label

    # -- resolution -----------------------------------------------------

    def get_by_name(self, name: str) -> PredefinedScript | None:
        """Найти скрипт по точному имени (PK). Возвращает ``None``, если нет."""
        if not name:
            return None
        rows = self._fetch_rows("name = ?", [name])
        if not rows:
            return None
        return self._row_to_script(rows[0])

    def list_all(self) -> list[PredefinedScript]:
        """Все зарегистрированные скрипты (отсортированы по ``name``)."""
        rows = self._fetch_rows(None, None)
        return [self._row_to_script(r) for r in rows]

    def find(
        self,
        query: str,
        *,
        top_k: int = 5,
    ) -> list[tuple[PredefinedScript, int]]:
        """Поиск скриптов по keyword-overlap с ``name + description + long_description``.

        Без ts_rank / embeddings: реестр маленький, нулевая latency, и
        ``ts_rank`` не является вероятностной метрикой — поэтому здесь
        простой и предсказуемый score (число общих токенов длиной >= 3).

        Args:
        query: NL-фраза для поиска.
        top_k: максимум результатов.

        Returns:
        Список ``(script, score)``, отсортированный по убыванию score.
        Пустой список, если реестр пуст или query пуст.
        """
        if not query or not query.strip():
            return []
        scripts = self.list_all()
        if not scripts:
            return []
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []

        scored: list[tuple[int, PredefinedScript]] = []
        for s in scripts:
            haystack_tokens = self._tokenize(
                f"{s.name} {s.description} {s.long_description}"
            )
            score = len(q_tokens & haystack_tokens)
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [(s, score) for score, s in scored[: max(0, top_k)]]

    # -- internals ------------------------------------------------------

    def _table_name(self) -> tuple[str, str]:
        """Имя таблицы реестра из ``TableRegistry.resources_by_label``.

        Returns:
        ``(schema, table)`` — например, ``("public", "agent_predefined_scripts")``.

        Raises:
        RuntimeError: если ни один ресурс не зарегистрирован под
            ``self._label``.
        """
        from lib.services.table_registry import table_registry

        resources = table_registry.resources_by_label(self._label)
        if not resources:
            raise RuntimeError(
                f"PredefinedScriptRegistry: ни один ресурс не зарегистрирован "
                f"с label={self._label!r}. Запустите через gateway "
                f"(ApplicationContext)."
            )
        full = resources[0].name
        if "." in full:
            schema, tbl = full.split(".", 1)
        else:
            schema, tbl = "main", full
        return schema, tbl

    def _fetch_rows(
        self,
        where: str | None,
        params: list | None,
    ) -> list[dict[str, Any]]:
        """Прочитать строки реестра из DuckDB-кеша."""
        schema, tbl = self._table_name()
        sql = (
            f'SELECT {self._REGISTRY_COLUMNS} FROM "{schema}"."{tbl}"'
        )
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY name"
        result = self._provider.query_sql(sql, params)
        if result.get("status") != "success":
            return []
        return list(result.get("rows") or [])

    @staticmethod
    def _row_to_script(row: dict[str, Any]) -> PredefinedScript:
        name = row.get("name") or ""
        if not name:
            raise ValueError("registry row without name")
        parameters = row.get("parameters") or {}
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters) or {}
            except (json.JSONDecodeError, TypeError):
                parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        normalised_params: dict[str, dict[str, Any]] = {}
        for pname, pdef in parameters.items():
            if isinstance(pdef, dict):
                normalised_params[str(pname)] = dict(pdef)
            else:
                normalised_params[str(pname)] = {"type": "string"}
        try:
            max_rows = int(row.get("max_rows_default") or 0)
        except (TypeError, ValueError):
            max_rows = 0
        return PredefinedScript(
            name=str(name),
            description=str(row.get("description") or ""),
            sql_template=str(row.get("sql_template") or ""),
            parameters=normalised_params,
            max_rows_default=max_rows,
            returns=str(row.get("returns") or ""),
            long_description=str(row.get("long_description") or ""),
            raw=dict(row),
        )

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {
            tok
            for tok in _TOKEN_SPLIT_RE.split((text or "").lower())
            if len(tok) >= 3
        }