"""Фабрика для ``LLMUsageStore`` (upstream nanobot).

Создаёт ``nanobot.llm_usage.store.LLMUsageStore`` по конфигурации
``gateway.usage_store.*``. Дефолтный путь —
``get_runtime_subdir("usage")/usage.db`` (см. design D4).

Если ``usage_config is None`` или ``usage_config.enabled=False`` —
возвращает ``None`` (graceful degradation; ``wrap_provider_snapshot_loader``
корректно обрабатывает ``store=None``, подключение observer не
выполняется, агент продолжает работу).

См. спеку ``openspec/specs/storage/usage-store/spec.md`` requirement
«Конфигурация UsageStore».
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger


def _ensure_dir(path: Path) -> None:
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def _default_sqlite_path() -> Path:
    """Default ``LLMUsageStore`` SQLite path.

    nanobot 0.3.5 не предоставляет ``nanobot.paths.get_runtime_subdir``;
    используем прямой fallback ``~/.cache/nanobot/usage/usage.db`` —
    совпадает с тем, что nanobot сам использует для ``get_data_dir``
    (см. ``nanobot/llm_usage/store.py: _DEFAULT_DB_NAME``).
    """
    return Path.home() / ".cache" / "nanobot" / "usage" / "usage.db"


def create_usage_store(
    usage_config: dict[str, Any] | None,
) -> Any | None:
    """Создать ``LLMUsageStore`` по конфигурации или вернуть ``None``.

    Args:
        usage_config: словарь из ``gateway.usage_store.*`` или ``None``.

    Returns:
        Экземпляр ``LLMUsageStore`` или ``None`` если конфигурация
        отсутствует / отключена.
    """
    if usage_config is None:
        return None
    if not usage_config.get("enabled", True):
        return None

    raw_path = usage_config.get("sqlite_path")
    if raw_path:
        sqlite_path = Path(str(raw_path)).expanduser()
    else:
        sqlite_path = _default_sqlite_path()

    _ensure_dir(sqlite_path)

    try:
        from nanobot.llm_usage.store import LLMUsageStore
    except ImportError as exc:
        logger.warning(
            "LLMUsageStore unavailable (nanobot import failed: {}); "
            "observer will not be attached",
            exc,
        )
        return None

    try:
        return LLMUsageStore(sqlite_path)
    except Exception as exc:
        logger.warning(
            "LLMUsageStore failed to initialize at {}: {}; "
            "observer will not be attached",
            sqlite_path,
            exc,
        )
        return None
