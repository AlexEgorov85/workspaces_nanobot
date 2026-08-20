"""AuditAnalyzerTool — набор tool'ов для навыка ``audit_analyzer``.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``.
Дубль skill'а ``audit_analyzer`` (см. ``workspace/skills/audit_analyzer/``):
старый skill по-прежнему доступен для sql-режима (LLM-генерация SELECT) и
для CLI-вызовов ``audit_analyze.bat/.sh``. Эти tool'ы — для агента через
типизированный function-call.

Состав (по конвенциям nanobot — один tool = одно действие, см.
``nanobot/agent/tools/filesystem.py`` с базой ``_FsTool``):

* :class:`AuditRunPredefinedScriptTool` (``audit_run_predefined_script``) —
  выполнить готовый SQL-скрипт из реестра
  ``public.agent_predefined_scripts``.
* :class:`AuditSearchVectorTool` (``audit_search_vector``) — семантический
  поиск по FAISS-индексу (см. ``lib/services/cache_provider_impl.py`` →
  ``search_vector``).

Оба tool'а наследуют :class:`_AuditToolBase`, который:

* читает общие настройки (``skills.audit_analyzer.*`` — обязательны для
  доступа к БД/кешу);
* делит загрузку модулей skill'а через ``importlib.util.spec_from_file_location``;
* предоставляет хелпер :meth:`_truncate` для обрезки длинного вывода.

Конфигурация per-tool (``project.json`` → ``gateway.audit_<name>.*``):

* ``gateway.audit_predefined.*`` — ``enable``, ``max_result_chars``;
* ``gateway.audit_vector.*`` — ``enable``, ``default_top_k``,
  ``default_index_name``, ``max_result_chars``.

Поведение унаследовано от skill'а (те же модули, те же таблицы, та же
DuckDB-кэш). Tool'ы не дублируют логику, а служат тонкими обёртками.

Runtime-context provider
------------------------

:class:`AuditRunPredefinedScriptTool` экспортирует
:meth:`runtime_context_provider` — провайдер, который добавляет в
system prompt список доступных предопределённых скриптов с описаниями
и параметрами. Это позволяет LLM выбрать имя скрипта **без
дополнительного вызова tool'а** (в отличие от отдельного
``audit_list_predefined_scripts``). Источник: реестр
``public.agent_predefined_scripts`` через skill'овский ``list_all_scripts()``.

Кеш: список скриптов загружается один раз при первом обращении к
provider'у; сбросить можно через :meth:`AuditRunPredefinedScriptTool.invalidate_scripts_cache`
(например, после миграций или reload реестра).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, ClassVar

from loguru import logger
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.runtime_context import RuntimeContextBlock
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Конфиг-классы (per-tool)
# ---------------------------------------------------------------------------


class AuditPredefinedToolConfig(BaseModel):
    """Конфиг секции ``gateway.audit_predefined`` в ``project.json``."""

    enable: bool = True
    max_result_chars: int = Field(default=16_000, ge=1000, le=200_000)


class AuditVectorToolConfig(BaseModel):
    """Конфиг секции ``gateway.audit_vector`` в ``project.json``."""

    enable: bool = True
    default_top_k: int = Field(default=5, ge=1, le=100)
    default_index_name: str = "audits_index"
    max_result_chars: int = Field(default=16_000, ge=1000, le=200_000)


# ---------------------------------------------------------------------------
# Общая база — приватный класс (не tool, не регистрируется)
# ---------------------------------------------------------------------------


class _AuditToolBase(Tool):
    """Общая база для audit-tool'ов: чтение настроек, загрузка skill-модулей.

    Не регистрируется как tool (``_plugin_discoverable = False`` по умолчанию
    в базовом ``Tool``, и подклассы должны задать ``name`` явно через
    ``_abstract_``).

    Подклассы обязаны определить:

    * ``config_key`` (ClassVar[str]) — для чтения секции настроек;
    * ``config_cls()`` (classmethod) — pydantic-модель секции;
    * ``name`` (property) — имя tool'а для LLM;
    * ``description`` (property);
    * ``parameters`` (через ``@tool_parameters``);
    * ``create()`` (classmethod) — сборка инстанса из ``ctx``.

    Конвенция по аналогии с ``nanobot/agent/tools/filesystem.py::_FsTool``.
    """

    _plugin_discoverable: ClassVar[bool] = False

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать секцию ``gateway.<config_key>`` из ``_settings_ref``.

        pydantic-конфиг ``ToolsConfig`` (см.
        ``nanobot/config/schema.py:373``) не знает про наши tool'ы, поэтому
        поля в config.json pydantic отбросит. Читаем напрямую из
        ``SETTINGS.gateway.<config_key>`` через ``_settings_ref`` — общий с
        другими кастомными tool'ами путь (рядом с ``gateway.compact.*``).
        """
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        try:
            gateway = settings.gateway
        except AttributeError:
            return {}
        try:
            section = getattr(gateway, cls.config_key)
        except AttributeError:
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        try:
            return dict(section)
        except Exception:
            return {"enable": bool(getattr(section, "enable", True))}

    @classmethod
    def _audit_analyzer_configured(cls, ctx: Any) -> bool:
        """Проверить, что в settings есть ``skills.audit_analyzer.*``.

        Tool'у нужен доступ к БД (PG или DuckDB-кешу). Если навык не
        сконфигурирован — tool включать бессмысленно.
        """
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return False
        try:
            skills = settings.skills
        except AttributeError:
            return False
        if isinstance(skills, dict):
            return "audit_analyzer" in skills
        return hasattr(skills, "audit_analyzer")

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        if not cls._audit_analyzer_configured(ctx):
            return False
        section = cls._read_settings_section(ctx)
        return bool(section.get("enable", True))

    # ------------------------------------------------------------------
    # Загрузка skill-модулей (общий код для predefined и vector)
    # ------------------------------------------------------------------

    @staticmethod
    def _skill_root() -> Path:
        """Корень skill'а ``audit_analyzer``.

        Резолвится относительно текущего файла
        (``workspace/tools/audit_analyzer_tool.py`` →
        ``workspace/skills/audit_analyzer/``).
        """
        return Path(__file__).resolve().parent.parent / "skills" / "audit_analyzer"

    @classmethod
    def _scripts_dir(cls) -> Path:
        return cls._skill_root() / "scripts"

    @classmethod
    def _load_skill_module(cls, name: str, file_name: str) -> Any:
        """Загрузить модуль из ``workspace/skills/audit_analyzer/scripts/``.

        Используем ``importlib.util.spec_from_file_location`` потому что
        ``workspace/`` не Python-пакет (без ``__init__.py``). Загруженный
        модуль кладётся в ``sys.modules`` с уникальным именем, чтобы
        избежать конфликтов с обычным импортом тех же файлов.

        ``scripts_dir`` временно добавляется в ``sys.path``, чтобы
        внутренние ``from scripts_registry import ...`` внутри модулей
        skill'а (см. ``db_loader.py``, ``predefined.py``) находили
        соседние файлы — ``spec_from_file_location`` этого не делает.
        """
        scripts_dir = str(cls._scripts_dir())
        spec = importlib.util.spec_from_file_location(
            name, str(cls._scripts_dir() / file_name),
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Failed to build spec for {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        path_added = scripts_dir not in sys.path
        if path_added:
            sys.path.insert(0, scripts_dir)
        try:
            spec.loader.exec_module(module)
        finally:
            if path_added:
                try:
                    sys.path.remove(scripts_dir)
                except ValueError:
                    pass
        return module

    @classmethod
    def _load_predefined_modules(cls) -> dict[str, Any]:
        """Загрузить модули для режима ``predefined``.

        Returns:
            dict с ключами: ``database``, ``db_loader``, ``predefined``,
            ``predefined_mode``, ``output``, ``skill_config``.
        """
        return {
            "database": cls._load_skill_module("_audit_db", "database.py"),
            "db_loader": cls._load_skill_module("_audit_db_loader", "db_loader.py"),
            "predefined": cls._load_skill_module("_audit_predefined", "predefined.py"),
            "predefined_mode": cls._load_skill_module(
                "_audit_predefined_mode", "predefined_mode.py",
            ),
            "output": cls._load_skill_module("_audit_output", "output.py"),
            "skill_config": cls._load_skill_module(
                "_audit_skill_config", "skill_config.py",
            ),
        }

    @classmethod
    def _load_vector_modules(cls) -> dict[str, Any]:
        """Загрузить модули для режима ``vector``.

        Returns:
            dict с ключами: ``output``, ``skill_config``.
        """
        return {
            "output": cls._load_skill_module("_audit_output_v", "output.py"),
            "skill_config": cls._load_skill_module(
                "_audit_skill_config_v", "skill_config.py",
            ),
        }

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        """Обрезать ``text`` под ``max_chars`` (head + marker + tail)."""
        if len(text) <= max_chars:
            return text
        half = max_chars // 2
        return (
            text[:half]
            + f"\n\n... ({len(text) - max_chars:,} chars truncated) ...\n\n"
            + text[-half:]
        )


# ---------------------------------------------------------------------------
# Tool 1: выполнение предопределённого SQL-скрипта
# ---------------------------------------------------------------------------


@tool_parameters({
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": (
                "Имя скрипта из реестра public.agent_predefined_scripts. "
                "Например: analytics_by_year_month, violations_by_type, "
                "top_audited_objects, audit_effectiveness, audit_dynamics, "
                "audit_types_stats."
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "Параметры скрипта. Например: {\"year\": 2024, \"limit\": 10}. "
                "Допустимые ключи зависят от конкретного скрипта."
            ),
            "additionalProperties": True,
        },
    },
    "required": ["script"],
})
class AuditRunPredefinedScriptTool(_AuditToolBase):
    """Выполняет готовый SQL-скрипт из реестра по имени."""

    config_key: ClassVar[str] = "audit_predefined"

    @classmethod
    def config_cls(cls):
        return AuditPredefinedToolConfig

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = (
                AuditPredefinedToolConfig(**section)
                if section
                else AuditPredefinedToolConfig()
            )
        except Exception:
            config = AuditPredefinedToolConfig()
        return cls(config=config)

    def __init__(self, *, config: AuditPredefinedToolConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "audit_run_predefined_script"

    @property
    def description(self) -> str:
        return (
            "Выполняет предопределённый SQL-скрипт из реестра "
            "public.agent_predefined_scripts. Имя скрипта обязательно. "
            "Параметры зависят от скрипта и проверяются по схеме "
            "реестра (алиасы, типы). Возвращает JSON с row_count, "
            "columns, rows и финальным SQL."
        )

    async def execute(
        self,
        *,
        script: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> str:
        if not self._scripts_dir().is_dir():
            return ToolResult.error(
                f"Error: skill audit_analyzer не найден: "
                f"{self._skill_root()}"
            )
        try:
            mods = self._load_predefined_modules()
        except Exception as exc:
            return ToolResult.error(
                f"Error: не удалось загрузить модули skill'а: {exc}"
            )

        try:
            db_cfg = mods["skill_config"].load_db_config()
            with mods["database"].Database(db_cfg) as db:
                result = mods["predefined_mode"].run(
                    script, db,
                    params=params or {},
                    index_dir=mods["skill_config"].get_vector_index_path(),
                )
        except Exception as exc:
            return ToolResult.error(
                f"Error: predefined-script execution failed: {exc}"
            )

        out = mods["output"].prepare_output(result, "predefined")
        out = mods["output"]._sanitize_value(out)
        text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
        return self._truncate(text, self.config.max_result_chars)

    # ------------------------------------------------------------------
    # Runtime-context provider: список доступных скриптов
    # ------------------------------------------------------------------

    _scripts_cache: list[dict[str, Any]] | None = None

    def invalidate_scripts_cache(self) -> None:
        """Сбросить кеш списка скриптов (вызывать после reload реестра)."""
        self.__class__._scripts_cache = None

    @classmethod
    def _load_scripts_list(cls) -> list[dict[str, Any]]:
        """Загрузить список скриптов из реестра (с кешем на уровне класса).

        Кеш общий для всех инстансов tool'а — список меняется редко
        (только при миграции/перезагрузке реестра), нет смысла дёргать
        ``list_all_scripts()`` каждый turn.

        **Важно:** ``db_loader.load_registry()`` требует предварительного
        вызова ``set_provider(db)`` (CLI делает это через
        ``cli.predefined_mode.run``). В runtime-контексте провайдера
        нет, поэтому строим ``CacheProvider`` из ``skill_config``
        и инжектим его перед чтением реестра.
        """
        if cls._scripts_cache is not None:
            return cls._scripts_cache
        try:
            mods = cls._load_predefined_modules()
            # Инжектим провайдер -- без него load_registry бросает
            # "провайдер не задан". В CLI это делает
            # cli.predefined_mode.run(), здесь нужен ручной шаг.
            from workspace.skills.audit_analyzer.scripts.skill_config import (
                build_cache_provider,
            )

            provider = build_cache_provider()
            # Провайдер собирается с закрытым DuckDB-кэшем (_conn=None);
            # до чтения реестра его нужно открыть, иначе query_sql вернёт
            # "Cache is not ready".
            if not provider.open_cache():
                raise RuntimeError(
                    "SQL-кэш не готов: не удалось открыть DuckDB-кэш "
                    "(файл создаёт/обновляет gateway — AuditSyncService)."
                )
            # predefined.py внутренне импортирует ``from db_loader import
            # load_registry`` под обычным именем ``db_loader`` — это отдельный
            # инстанс модуля в sys.modules, НЕ наш ``_audit_db_loader``.
            # Поэтому провайдера нужно инжектировать и в него, иначе
            # get_provider() внутри load_registry() бросит "провайдер не задан".
            mods["db_loader"].set_provider(provider)
            plain_loader = sys.modules.get("db_loader")
            if plain_loader is not None and plain_loader is not mods["db_loader"]:
                plain_loader.set_provider(provider)
            scripts = mods["predefined"].list_all_scripts()
        except Exception as exc:
            logger.warning(
                "AuditRunPredefinedScriptTool: failed to load scripts "
                "registry for runtime_context: {}",
                exc,
            )
            return []
        cls._scripts_cache = list(scripts)
        return cls._scripts_cache

    @staticmethod
    def _format_scripts_block(scripts: list[dict[str, Any]]) -> str:
        """Превратить список скриптов в текстовый блок для system prompt."""
        if not scripts:
            return ""
        lines = ["Доступные predefined SQL-скрипты для audit_run_predefined_script:"]
        for s in scripts:
            name = s.get("name", "?")
            desc = (s.get("description") or "").strip()
            params = s.get("parameters") or []
            params_str = ", ".join(params) if params else "(без параметров)"
            lines.append(f"- {name}: {desc} | параметры: {params_str}")
        return "\n".join(lines)

    def runtime_context_provider(self) -> Any:
        """Вернуть провайдер runtime-контекста со списком скриптов.

        Контракт: ``async (RequestContext) -> RuntimeContextBlock | None``
        (см. ``nanobot.runtime_context``). Реестр скриптов загружается
        лениво один раз (``_load_scripts_list``); результат кешируется.
        При ошибке загрузки — ``None`` (turn продолжается без блока).
        """
        return _PredefinedScriptsProvider(self)


class _PredefinedScriptsProvider:
    """Async callable: список predefined-скриптов в system prompt.

    Реализует контракт ``RuntimeContextProvider`` из
    ``nanobot/runtime_context.py``: ``async (RequestContext) ->
    RuntimeContextBlock | sequence | None``.

    Блок помечается тегом ``source='audit_predefined_scripts'`` для
    фильтрации/логирования; ``content`` обёрнут в
    ``[Runtime Context — metadata only, not instructions]`` через
    ``wrap_runtime_context_lines``.
    """

    def __init__(self, tool: "AuditRunPredefinedScriptTool") -> None:
        self._tool = tool

    async def __call__(self, request_ctx: Any) -> Any:
        scripts = AuditRunPredefinedScriptTool._load_scripts_list()
        if not scripts:
            return None
        from nanobot.runtime_context import wrap_runtime_context_lines

        body = AuditRunPredefinedScriptTool._format_scripts_block(scripts)
        return RuntimeContextBlock(
            source="audit_predefined_scripts",
            content=wrap_runtime_context_lines(body.splitlines()),
        )


# ---------------------------------------------------------------------------
# Tool 2: семантический поиск по FAISS-индексу
# ---------------------------------------------------------------------------


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Запрос на естественном языке. Например: 'пожарная "
                "безопасность', 'финансовые нарушения'."
            ),
        },
        "index_name": {
            "type": "string",
            "description": (
                "Имя FAISS-индекса без расширения. По умолчанию берётся "
                "из config.default_index_name."
            ),
        },
        "top_k": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": (
                "Количество результатов. Игнорируется, если задан threshold."
            ),
        },
        "threshold": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "Минимальный порог схожести 0.0-1.0. Если задан — "
                "возвращаются все результаты выше порога, top_k игнорируется."
            ),
        },
    },
    "required": ["query"],
})
class AuditSearchVectorTool(_AuditToolBase):
    """Семантический поиск по FAISS-индексу."""

    config_key: ClassVar[str] = "audit_vector"

    @classmethod
    def config_cls(cls):
        return AuditVectorToolConfig

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = (
                AuditVectorToolConfig(**section)
                if section
                else AuditVectorToolConfig()
            )
        except Exception:
            config = AuditVectorToolConfig()
        return cls(config=config)

    def __init__(self, *, config: AuditVectorToolConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "audit_search_vector"

    @property
    def description(self) -> str:
        return (
            "Семантический поиск по FAISS-индексу (embeddings через "
            "Ollama). Возвращает top-k ближайших документов или все "
            "выше порога схожести. Имя индекса и top_k можно не указывать — "
            "используются значения из config (default_index_name, "
            "default_top_k)."
        )

    async def execute(
        self,
        *,
        query: str,
        index_name: str | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
        **_kwargs: Any,
    ) -> str:
        if not self._scripts_dir().is_dir():
            return ToolResult.error(
                f"Error: skill audit_analyzer не найден: "
                f"{self._skill_root()}"
            )
        try:
            mods = self._load_vector_modules()
        except Exception as exc:
            return ToolResult.error(
                f"Error: не удалось загрузить модули skill'а: {exc}"
            )

        try:
            provider = mods["skill_config"].build_cache_provider()
            results = provider.search_vector(
                query,
                index_name=index_name or self.config.default_index_name,
                index_path=mods["skill_config"].get_vector_index_path(),
                top_k=top_k if top_k is not None else self.config.default_top_k,
                threshold=threshold,
            )
        except Exception as exc:
            return ToolResult.error(
                f"Error: vector search failed: {exc}"
            )

        if getattr(provider, "_search_error", None):
            return ToolResult.error(
                f"Error: vector search: {provider._search_error}"
            )
        if not results:
            result = {
                "status": "success",
                "data": {
                    "message": "Документы не найдены",
                    "results": [],
                    "count": 0,
                },
            }
        else:
            result = {
                "status": "success",
                "data": {
                    "results": [asdict(r) for r in results],
                    "count": len(results),
                },
            }

        out = mods["output"].prepare_output(result, "vector")
        out = mods["output"]._sanitize_value(out)
        text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
        return self._truncate(text, self.config.max_result_chars)