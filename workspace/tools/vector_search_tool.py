"""``vector_search`` — generic semantic search tool.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Контракт и поведение описаны в ``docs/skill-tool-architecture.md`` §7.

Конфиг читается из секции ``gateway.vector_search.*`` в ``project.json``::

    {
      "gateway": {
        "vector_search": {
          "enable": true,
          "default_top_k": 5,
          "max_top_k": 50,
          "default_threshold": 0.0,
          "max_query_chars": 4000,
          "max_result_chars": 16000,
          "timeout_sec": 30
        }
      }
    }

Инфраструктура выполнения поиска:
  * ``lib/services/cache_provider.py::CacheProvider.search_vector`` —
    абстрактный интерфейс к FAISS-индексам (конкретная реализация
    выбирается приложением).
  * ``lib/utils/text_utils.py::truncate_middle`` — обрезка ответа.
  * ``lib/utils/text_utils.py::sanitize_value`` — JSON-сериализация.

Observability покрывается штатным ``lib/hooks/tool_audit_hook.py``
(см. TARGET_ARCHITECTURE.md §26) — этот tool не дублирует логирование.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Optional

from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel, Field

from lib.services.cache_provider import IndexIntegrityError
from lib.utils.text_utils import sanitize_value, truncate_middle


def _available_index_names() -> str:
    """Список имён индексов из ``public.agent_vector_index_config`` (PG).

    Для подсказок в error-сообщениях tool'а: вместо hardcoded-домен-имён
    показывает реально зарегистрированные индексы из runtime-БД.
    При недоступной PG / пустой конфигурации — fallback без падения.
    """
    try:
        from lib.services.cache_provider_impl import read_vector_index_config

        names = sorted(read_vector_index_config({}).keys())
        if names:
            return ", ".join(names)
        return (
            "(нет индексов в public.agent_vector_index_config — "
            "настройте gateway.vector.index.storage_table и "
            "tools/build_vectors.py)"
        )
    except Exception as exc:
        return f"(не удалось прочитать agent_vector_index_config: {exc})"


class VectorSearchToolConfig(BaseModel):
    """Конфиг секции ``gateway.vector_search`` в ``project.json``."""

    enable: bool = True
    default_top_k: int = Field(default=5, ge=1, le=100)
    max_top_k: int = Field(default=50, ge=1, le=100)
    default_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    max_query_chars: int = Field(default=4000, ge=10, le=16000)
    max_result_chars: int = Field(default=16_000, ge=100, le=200_000)
    timeout_sec: int = Field(default=30, ge=1, le=120)


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Поисковый запрос на естественном языке. "
                "Преобразуется в embedding и сравнивается с векторами индекса."
            ),
        },
        "index_name": {
            "type": "string",
            "description": "Имя векторного индекса (конфигурируется в CacheProvider).",
        },
        "top_k": {
            "type": "integer",
            "description": (
                "Сколько ближайших результатов вернуть. "
                "Не больше ``max_top_k`` из конфигурации."
            ),
            "minimum": 1,
        },
        "threshold": {
            "type": "number",
            "description": (
                "Минимальная схожесть (cosine) в диапазоне [0, 1]. "
                "Если не указана — используется ``default_threshold``."
            ),
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
    "required": ["query", "index_name"],
})
class VectorSearchTool(Tool):
    """Семантический поиск по указанному векторному индексу."""

    config_key: ClassVar[str] = "vector_search"

    def __init__(self, *, config: VectorSearchToolConfig) -> None:
        self.config = config
        self._provider: Optional[Any] = None

    def set_provider(self, provider: Any) -> None:
        """Установить CacheProvider (для DI в тестах).

        Provider должен реализовать ``search_vector(query, index_name, ...)``.
        """
        self._provider = provider

    @classmethod
    def config_cls(cls):
        return VectorSearchToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать ``gateway.vector_search`` из ``ctx._settings_ref``."""
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        try:
            gateway = settings.gateway
        except AttributeError:
            return {}
        if gateway is None:
            return {}
        try:
            section = getattr(gateway, cls.config_key)
        except AttributeError:
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        out: dict[str, Any] = {}
        for field in ("enable", "default_top_k", "max_top_k",
                      "default_threshold", "max_query_chars",
                      "max_result_chars", "timeout_sec"):
            if hasattr(section, field):
                out[field] = getattr(section, field)
        if not out:
            try:
                out = dict(vars(section))
            except Exception:
                pass
        return out

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        section = cls._read_settings_section(ctx)
        return bool(section.get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = cls.config_cls()(**section)
        except Exception:
            config = cls.config_cls()()
        return cls(config=config)

    @property
    def name(self) -> str:
        return "vector_search"

    @property
    def description(self) -> str:
        return (
            "Search a configured vector index using semantic similarity. "
            "Returns the top-k nearest documents with score, text, and "
            "metadata. The caller is responsible for choosing the right "
            "index_name."
        )

    async def execute(
        self,
        *,
        query: str,
        index_name: str,
        top_k: Optional[int] = None,
        threshold: Optional[float] = None,
        **_kwargs: Any,
    ) -> str:
        """Выполнить семантический поиск и вернуть JSON-сериализованный ответ."""
        if not isinstance(query, str) or not query.strip():
            return self._error("invalid_input", "query is empty")
        if not isinstance(index_name, str) or not index_name.strip():
            return self._error("invalid_input", "index_name is empty")
        if len(query) > self.config.max_query_chars:
            return self._error(
                "invalid_input",
                f"query length {len(query)} exceeds max_query_chars "
                f"{self.config.max_query_chars}",
            )

        effective_top_k = top_k or self.config.default_top_k
        if effective_top_k > self.config.max_top_k:
            return self._error(
                "invalid_input",
                f"top_k={effective_top_k} exceeds max_top_k "
                f"{self.config.max_top_k}",
            )

        effective_threshold = (
            threshold if threshold is not None else self.config.default_threshold
        )
        if not 0.0 <= effective_threshold <= 1.0:
            return self._error(
                "invalid_input",
                f"threshold={effective_threshold} not in [0, 1]",
            )

        provider = getattr(self, "_provider", None)
        if provider is None:
            return self._error(
                "missing_provider",
                "vector_search tool не подключён к CacheProvider "
                "(см. RuntimePatcher.patch_project_tools и "
                "gateway.vector.index.storage_table в project.json). "
                f"Available indexes from settings: {_available_index_names()}",
            )

        try:
            raw_results = provider.search_vector(
                query=query,
                index_name=index_name,
                top_k=effective_top_k,
                threshold=effective_threshold,
            )
        except AttributeError:
            return self._error(
                "missing_index",
                f"index '{index_name}' is not registered or unreachable. "
                f"Available indexes from settings: {_available_index_names()}",
            )
        except IndexIntegrityError as exc:
            # Устаревший/повреждённый векторный индекс блокируется провайдером;
            # клиент видит конкретный error_type и reason для пересборки.
            return self._error(
                f"{exc.status.lower()}_index",
                f"vector index '{exc.index_name}' is {exc.status}: {exc.reason}. "
                f"Available indexes from settings: {_available_index_names()}",
            )
        except Exception as exc:
            return self._error("search_failure", str(exc))

        results = self._normalize_results(raw_results, index_name)
        payload = {
            "status": "success",
            "index_name": index_name,
            "query": query,
            "results": results,
            "count": len(results),
            "truncated": False,
        }
        # STALE/INVALID detection — поставщик (provider) помечает meta через
        # ``_signature_status`` при загрузке индекса из store. Если статус
        # не CURRENT — поиск работает, но клиент видит warning с причиной
        # и рекомендацией пересобрать индекс.
        sig_status = (raw_results.__class__.__name__ and None) or None
        meta = getattr(self._provider, "_last_loaded_meta", None)
        if isinstance(meta, dict):
            sig_status = meta.get("_signature_status")
            sig_reason = meta.get("_signature_reason")
            if sig_status and sig_status != "CURRENT":
                payload["index_warning"] = {
                    "status": sig_status,
                    "reason": sig_reason,
                    "recommendation": (
                        "rebuild index via tools/build_vectors.py"
                    ),
                }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        text = truncate_middle(text, self.config.max_result_chars)
        return text

    def _normalize_results(self, raw: Any, index_name: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in raw or []:
            if hasattr(item, "content"):
                row = getattr(item, "row", None) or {}
                out.append({
                    "id": sanitize_value(getattr(item, "pk_value", None)),
                    "score": float(getattr(item, "score", 0.0) or 0.0),
                    "text": sanitize_value(getattr(item, "content", "")),
                    "metadata": {
                        "source": getattr(item, "source", ""),
                        "table": getattr(item, "table", ""),
                        "chunk_index": getattr(item, "chunk", ""),
                        "matched_chunks": getattr(item, "matched_chunks", 1),
                        "index_name": index_name,
                        "row": sanitize_value(row) if isinstance(row, dict) else {},
                    },
                })
            elif isinstance(item, dict):
                out.append({
                    "id": sanitize_value(item.get("id") or item.get("pk_value")),
                    "score": float(item.get("score", 0.0) or 0.0),
                    "text": sanitize_value(item.get("text") or item.get("content") or ""),
                    "metadata": sanitize_value(item.get("metadata") or {}),
                })
        return out

    def _error(self, error_type: str, message: str) -> str:
        payload = {
            "status": "error",
            "error_type": error_type,
            "message": message,
        }
        return json.dumps(payload, ensure_ascii=False)