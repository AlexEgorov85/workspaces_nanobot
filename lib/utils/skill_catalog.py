"""Internal helper: рендерит секции каталога в SKILL.md.

Не tool. Источник — auto-populated env-vars ``SKILL_<NAME>_*``,
которые выставляет ``ApplicationContext._populate_skill_catalog_env``
при старте/инвалидации.

Env-vars (auto-populated, не задаются вручную в .secrets.env)::

    SKILL_<NAME>_TABLES              = "schema.table1,schema.table2,..."
    SKILL_<NAME>_VECTORS             = "index_name1,index_name2,..."
    SKILL_<NAME>_SCRIPTS             = "script_name1,..."
    SKILL_<NAME>_SCRIPT_DESCRIPTIONS = "name1=desc1;name2=desc2;..."
    SKILL_<NAME>_VECTOR_DESCRIPTIONS = "name1=desc1;name2=desc2;..."

Маркеры в SKILL.md::

    {{SCRIPTS_CATALOG}}   — таблица predefined scripts
    {{VECTORS_CATALOG}}   — таблица vector indexes
    {{TABLES_CATALOG}}    — таблица таблиц (опц., нужен для NL→SQL capability)

Архитектурный контракт:
    * skill_catalog — internal helper, не часть публичного API skill'а;
    * рендерит markdown-таблицу в ``format_for_skill_md``;
    * неизвестные маркеры остаются как есть (forward-compat);
    * пустые env-vars → placeholder ``*(не зарегистрировано)*``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final


__all__ = ["SkillCatalog"]


_MARKER_RE: Final[dict[str, re.Pattern[str]]] = {
    marker: re.compile(r"\{\{" + marker + r"\}\}")
    for marker in ("SCRIPTS_CATALOG", "VECTORS_CATALOG", "TABLES_CATALOG")
}

_TABLE_HEADERS: Final[dict[str, tuple[str, str]]] = {
    "SCRIPTS_CATALOG": ("Script", "Описание"),
    "VECTORS_CATALOG": ("Index", "Описание"),
    "TABLES_CATALOG": ("Table", ""),
}


class SkillCatalog:
    """Рендерит каталожные секции SKILL.md из auto-populated env-vars.

    Все методы класса — статические; экземпляр не создаётся.
    Подмешивается ``RuntimePatcher.patch_skill_catalogs`` при загрузке
    SKILL.md из nanobot.
    """

    @classmethod
    def render_expanded_skill(cls, skill_name: str, template: str) -> str:
        """Заменить ``{{SCRIPTS_CATALOG}}``, ``{{VECTORS_CATALOG}}``, ``{{TABLES_CATALOG}}``
        на rendered-таблицы из env-vars ``SKILL_<NAME>_*``.

        Args:
            skill_name: имя skill'а (например, ``"audit_analyzer"``).
                Используется для построения env-prefix ``SKILL_<UPPER>_*``.
            template: исходный текст SKILL.md с маркерами.

        Returns:
            Новый текст с заменёнными маркерами. Неизвестные маркеры
            (``{{...}}``, не входящие в ``_MARKER_RE``) остаются как есть.
        """
        upper = cls._skill_to_env_suffix(skill_name)
        out = template
        out = _MARKER_RE["SCRIPTS_CATALOG"].sub(
            lambda _m: cls._render_named_table(upper, "SCRIPTS_CATALOG"), out
        )
        out = _MARKER_RE["VECTORS_CATALOG"].sub(
            lambda _m: cls._render_named_table(upper, "VECTORS_CATALOG"), out
        )
        out = _MARKER_RE["TABLES_CATALOG"].sub(
            lambda _m: cls._render_named_table(upper, "TABLES_CATALOG"), out
        )
        return out

    @classmethod
    def read_tables(cls, skill_name: str) -> list[str]:
        """Список таблиц из ``SKILL_<NAME>_TABLES``. Только для тестов/отладки."""
        return cls._read_csv(cls._env_key(skill_name, "TABLES"))

    @classmethod
    def read_vectors(cls, skill_name: str) -> list[str]:
        """Список vector indexes из ``SKILL_<NAME>_VECTORS``. Только для тестов/отладки."""
        return cls._read_csv(cls._env_key(skill_name, "VECTORS"))

    @classmethod
    def read_scripts(cls, skill_name: str) -> list[str]:
        """Список predefined scripts из ``SKILL_<NAME>_SCRIPTS``. Только для тестов/отладки."""
        return cls._read_csv(cls._env_key(skill_name, "SCRIPTS"))

    @classmethod
    def refresh_runtime_catalog(cls, workspace_path: Path | None = None) -> None:
        """Перечитать runtime-каталог и обновить ``SKILL_<NAME>_*`` env-vars.

        Используется, когда источники каталога появились **после** старта
        процесса: cold-start, когда ``PgDuckDbSyncService.initial_load``
        публикует DuckDB-снапшот асинхронно — первичный populate в
        ``ApplicationContext.start`` ещё видел пустой snapshot, а
        once-callback на ``on_sync`` вызывает этот метод повторно.

        Args:
            workspace_path: корень workspace (для поиска DuckDB-снапшота).
                ``None`` — авто-определение (cwd / env-vars).

        Контракт:
            * идемпотентен: повторный вызов с тем же состоянием даёт
              идентичные env-vars;
            * не создаёт дополнительных соединений / инфраструктуры;
            * не выполняет PG-запросов;
            * работает только с уже существующим snapshot-файлом.
        """
        from lib.core.application_context import _populate_skill_catalog_env

        _populate_skill_catalog_env(workspace_path=workspace_path)

    @classmethod
    def clear_skill_env(cls, skill_name: str | None = None) -> int:
        """Удалить env-vars ``SKILL_<NAME>_*`` из ``os.environ``.

        Args:
            skill_name: удалить только для этого skill'а (``None`` — все).

        Returns:
            Число удалённых переменных.
        """
        if skill_name is None:
            prefix = "SKILL_"
            keys = [k for k in os.environ if k.startswith(prefix)]
        else:
            suffix = cls._skill_to_env_suffix(skill_name)
            prefix = f"SKILL_{suffix}_"
            keys = [k for k in os.environ if k.startswith(prefix)]
        for k in keys:
            del os.environ[k]
        return len(keys)

    # ---------- private -------------------------------------------------

    @classmethod
    def _render_named_table(cls, upper: str, marker: str) -> str:
        """Render markdown-таблицу для одного маркера."""
        if marker == "SCRIPTS_CATALOG":
            names = cls._read_csv(f"SKILL_{upper}_SCRIPTS")
            descs = cls._read_descriptions(f"SKILL_{upper}_SCRIPT_DESCRIPTIONS")
        elif marker == "VECTORS_CATALOG":
            names = cls._read_csv(f"SKILL_{upper}_VECTORS")
            descs = cls._read_descriptions(f"SKILL_{upper}_VECTOR_DESCRIPTIONS")
        elif marker == "TABLES_CATALOG":
            names = cls._read_csv(f"SKILL_{upper}_TABLES")
            descs = {}
        else:
            return ""

        if not names:
            return "*(нет зарегистрированных ресурсов)*\n"

        col1, col2 = _TABLE_HEADERS[marker]
        if col2:
            header = f"| {col1} | {col2} |"
            sep = "|---|---|"
            rows = []
            for n in names:
                d = descs.get(n, "").replace("|", "\\|").replace("\n", " ")
                rows.append(f"| `{n}` | {d} |")
        else:
            header = f"| {col1} |"
            sep = "|---|"
            rows = [f"| `{n}` |" for n in names]

        return "\n".join([header, sep, *rows]) + "\n"

    @staticmethod
    def _env_key(skill_name: str, suffix: str) -> str:
        return f"SKILL_{SkillCatalog._skill_to_env_suffix(skill_name)}_{suffix}"

    @staticmethod
    def _skill_to_env_suffix(skill_name: str) -> str:
        return skill_name.upper().replace("-", "_")

    @staticmethod
    def _read_csv(key: str) -> list[str]:
        raw = os.environ.get(key, "")
        return [x.strip() for x in raw.split(",") if x.strip()]

    @staticmethod
    def _read_descriptions(key: str) -> dict[str, str]:
        raw = os.environ.get(key, "")
        out: dict[str, str] = {}
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k and v:
                    out[k] = v
        return out
