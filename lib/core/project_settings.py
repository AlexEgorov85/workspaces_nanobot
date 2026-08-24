"""ProjectSettings — типизированная валидация проектных настроек (pydantic).

Fail-fast граница конфигурации: неправильный тип или недопустимое значение
ключа ``project.json`` ловится на старте приложения (``ApplicationContext.
create``), а не в рантайме канала/сервиса.

Принципы:
  - все ключи опциональны с дефолтами: отсутствие настройки не ошибка
    (дефолты живут в потребителях через ``get_setting``);
  - неверный ТИП или значение — ошибка: ``ConfigurationError`` со списком
    всех проблем сразу;
  - неизвестные ключи разрешены (extra="allow") — forward-совместимость;
  - единственный источник правды — SETTINGS после мержа
    project.json → config.json → .secrets.env.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import ConfigurationError

__all__ = ["ProjectSettings", "validate_project_settings"]


class _StrictOptional(BaseModel):
    """База для секций: неизвестные ключи разрешены, известные — типизированы."""

    model_config = ConfigDict(extra="allow")


class PostgresChannelSettings(_StrictOptional):
    worker_id: str | None = None
    claims_table: str | None = None
    claim_strategy: Literal["single", "worker_pool"] | None = None
    poll_interval: float | None = Field(default=None, gt=0)
    lease_interval: float | None = Field(default=None, gt=0)
    error_retry_delay: float | None = Field(default=None, ge=0)
    unstick_interval: int | None = Field(default=None, gt=0)
    processing_timeout: int | None = Field(default=None, gt=0)


class CompactSettings(_StrictOptional):
    enabled: bool | None = None
    notify_in_history: bool | None = None
    print_to_terminal: bool | None = None


class DuckDbQuerySettings(_StrictOptional):
    enable: bool | None = None
    max_rows: int | None = Field(default=None, gt=0)
    max_result_chars: int | None = Field(default=None, gt=0)
    query_timeout_sec: float | None = Field(default=None, gt=0)


class VectorSearchSettings(_StrictOptional):
    enable: bool | None = None
    default_top_k: int | None = Field(default=None, gt=0)
    max_top_k: int | None = Field(default=None, gt=0)
    default_threshold: float | None = Field(default=None, ge=0, le=1)
    max_query_chars: int | None = Field(default=None, gt=0)
    max_result_chars: int | None = Field(default=None, gt=0)
    timeout_sec: float | None = Field(default=None, gt=0)


class HeartbeatSettings(_StrictOptional):
    enabled: bool | None = None
    intervalS: int | None = Field(default=None, gt=0)


class GatewaySettings(_StrictOptional):
    print_llm_calls: bool | None = None
    print_worker_activity: bool | None = None
    print_db_activity: bool | None = None
    llm_timeout: int | None = Field(default=None, gt=0)
    exec_timeout: int | None = Field(default=None, gt=0)
    compact: CompactSettings | None = None
    duckdb_query: DuckDbQuerySettings | None = None
    vector_search: VectorSearchSettings | None = None
    heartbeat: HeartbeatSettings | None = None


class CliSettings(_StrictOptional):
    show_context_window: bool | None = None
    max_iterations: int | None = Field(default=None, gt=0)


class StreamlitSettings(_StrictOptional):
    enabled: bool | None = None
    error_window_sec: float | None = Field(default=None, gt=0)


class ChannelsSettings(_StrictOptional):
    postgres: PostgresChannelSettings | None = None


class LoggingDbSettings(_StrictOptional):
    enabled: bool | None = None


class LoggingSettings(_StrictOptional):
    db: LoggingDbSettings | None = None


class ProjectSettings(BaseModel):
    """Корневая модель проектных настроек (проекция секций SETTINGS)."""

    model_config = ConfigDict(extra="allow")

    version: str | None = None
    channels: ChannelsSettings | None = None
    gateway: GatewaySettings | None = None
    cli: CliSettings | None = None
    streamlit: StreamlitSettings | None = None
    logging: LoggingSettings | None = None


def validate_project_settings(settings: Any) -> ProjectSettings:
    """Валидировать SETTINGS; вернуть типизированную проекцию.

    Args:
        settings: merged SETTINGS (AttrDict/dict) из ``config.py``.

    Returns:
        ``ProjectSettings`` с распарсенными секциями.

    Raises:
        ConfigurationError: если хотя бы один известный ключ имеет неверный
            тип или недопустимое значение; сообщение содержит ВСЕ проблемы.
    """
    try:
        return ProjectSettings.model_validate(dict(settings or {}))
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            path = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "invalid")
            input_val = repr(err.get("input"))[:80]
            problems.append(f"  {path}: {msg} (получено: {input_val})")
        raise ConfigurationError(
            "Некорректная конфигурация project.json:\n" + "\n".join(problems)
        ) from exc
