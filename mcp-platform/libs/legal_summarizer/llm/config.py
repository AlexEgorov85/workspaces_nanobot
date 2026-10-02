"""Настройки домена ``legal_summarizer``.

**Почему не чтение настроек из окружения.** До переноса этот модуль был
тонкой обёрткой над ``lib.core.skill_config`` агента, а тот — над глобальным
``config.SETTINGS``. Такая схема требовала, чтобы entrypoint агента успел
вызвать ``_initialize_settings(profile)``; вне entrypoint (тесты, capability)
она падала с ``ConfigurationError: SETTINGS not initialized``, и это была
причина 182 падений в прогонах скилла.

Теперь значения принадлежат домену: они объявлены здесь как
:class:`LegalConfig` с документированными дефолтами, а внешний мир может
подставить свои через :func:`configure` на границе capability. Ни окружение,
ни файлы модуль не читает.

Дефолты перенесены из ``project.json::skills.legal_summarizer`` без
изменений. Ключи, которых в ``project.json`` не было, читатели продолжают
брать своими собственными ``.get(key, default)`` — этот модуль их не
дублирует и не выдумывает.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = (
    "LegalConfig",
    "configure",
    "current",
    "get_brief_context_config",
    "get_chunking_config",
    "get_cli_config",
    "get_context_window_tokens",
    "get_default_length",
    "get_execution_config",
    "get_identity",
    "llm_trace_enabled",
    "mr_trace_enabled",
    "get_llm_config",
    "get_max_retries",
    "get_timeout_sec",
    "reset",
)


@dataclass(frozen=True)
class LegalConfig:
    """Настройки домена. Секции — как в ``platform.json`` / ``project.json``.

    Attributes:
        cli: параметры запуска — ``default_length``, ``timeout_sec``,
            ``max_retries``.
        llm: бюджет одного вызова — ``max_tokens``, ``temperature``.
        chunking: пороги map-reduce; ``chunk_size_input_ratio`` — доля
            контекстного окна под один чанк, ``chunk_size`` — fallback.
        brief_context: параметры ``BriefContextBuilder``.
        execution: политика длинных операций и safety net.
        context_window_tokens: окно модели в токенах. ``None`` означает
            «неизвестно», и размер чанка берётся из ``chunking.chunk_size``.
            Домен не читает это из настроек модели: объявляет capability
            ``llm``.
    """

    cli: Mapping[str, Any] = field(default_factory=dict)
    llm: Mapping[str, Any] = field(default_factory=dict)
    chunking: Mapping[str, Any] = field(default_factory=dict)
    brief_context: Mapping[str, Any] = field(default_factory=dict)
    execution: Mapping[str, Any] = field(default_factory=dict)
    context_window_tokens: int | None = None
    mr_trace: bool = False
    llm_trace: bool = False
    identity: Mapping[str, str] = field(default_factory=dict)


#: Дефолты — значения, которые агент держал в
#: ``project.json::skills.legal_summarizer`` на момент переноса (2026-10-02).
DEFAULTS = LegalConfig(
    cli={
        "default_length": "brief",
        "timeout_sec": 120,
        "max_retries": 3,
    },
    llm={
        "max_tokens": 8192,
        "temperature": 0.1,
    },
    chunking={
        "chunk_size_input_ratio": 0.5,
        "chunk_size": 100000,
        "chunk_overlap": 0,
        "single_call_threshold": 20000,
        "brief_input_ratio": 0.13,
    },
    brief_context={
        "max_chars_fallback": 30000,
        "chars_per_token": 3.5,
        "structure_max_chars": 12000,
    },
    execution={
        "confirmation_threshold_sec": 120,
        "estimated_chunk_duration_sec": 20,
        "max_chunks_for_execution": 50,
        "max_concurrent_batches": 1,
        "max_chunks_per_question": 10,
        "context_batching": {
            "chars_per_token": 3.5,
            "system_prompt_tokens": 1200,
            "instruction_tokens_per_map": 200,
            "safety_margin": 0.85,
            "llm_max_tokens": 8192,
        },
    },
    context_window_tokens=None,
)

_active: LegalConfig = DEFAULTS


def configure(config: LegalConfig | None) -> None:
    """Подставить настройки домена.

    Вызывается на границе capability, когда сервер уже разобрал свои
    настройки. ``None`` возвращает домен к дефолтам.
    """
    global _active
    _active = DEFAULTS if config is None else config


def reset() -> None:
    """Вернуть дефолты. Для тестов."""
    configure(None)


def current() -> LegalConfig:
    """Действующая конфигурация."""
    return _active


def with_overrides(**sections: Mapping[str, Any]) -> LegalConfig:
    """Копия текущей конфигурации с заменёнными секциями.

    Только для тестов и для точечной подмены: полная замена делается
    через :func:`configure`.
    """
    return replace(_active, **sections)


def get_cli_config() -> dict[str, Any]:
    return dict(_active.cli)


def get_llm_config() -> dict[str, Any]:
    return dict(_active.llm)


def get_max_retries() -> int:
    return int(_active.cli.get("max_retries", 3))


def get_chunking_config() -> dict[str, Any]:
    return dict(_active.chunking)


def get_brief_context_config() -> dict[str, Any]:
    """Параметры ``BriefContextBuilder``."""
    return dict(_active.brief_context)


def get_execution_config() -> dict[str, Any]:
    """Политика длинных операций, safety net и context batching."""
    return dict(_active.execution)


def get_context_window_tokens() -> int | None:
    """Окно модели в токенах либо ``None``, если оно неизвестно."""
    return _active.context_window_tokens


def get_default_length() -> str:
    return str(_active.cli.get("default_length", "medium"))


def mr_trace_enabled() -> bool:
    """Отладочный флаг трассировки map-reduce.

    Раньше читался из ``LEGAL_SUMMARIZER_MR_TRACE`` в окружении. На платформе
    окружение читает только реестр, а флаг - свойство домена, поэтому он
    приходит настройкой.
    """
    return _active.mr_trace


def llm_trace_enabled() -> bool:
    """Отладочный флаг трассировки LLM-вызовов (было ``LEGAL_SUMMARIZER_LLM_TRACE``)."""
    return _active.llm_trace


def get_identity() -> dict[str, str]:
    """Идентичность оборота: ``session_id`` / ``user_id`` / ``request_id``.

    Раньше читалась из ``ENTERPRISE_*`` в окружении подпроцесса - так было,
    пока скилл запускался агентом отдельным процессом. Теперь домен живёт в
    платформе, читать окружение напрямую нельзя, а окончательным источником
    должен стать контракт операции (п. 8.7a/8.7c). Пусто - оборот вне
    контекста: ``_meta`` не уйдёт вовсе, и сервер ответит
    ``identity_missing``, что точнее выдуманной сессии в журнале.
    """
    return dict(_active.identity)


def get_timeout_sec() -> float:
    return float(_active.cli.get("timeout_sec", 120))
