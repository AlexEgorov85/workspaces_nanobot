"""ConfigService — единая точка загрузки конфигурации проекта.

Отвечает за:
  * доступ к глобальным ``SETTINGS`` (собираются в ``config.py``:
    project.json → config.json → .secrets.env, резолв ``${VAR}``);
  * загрузку runtime-конфига nanobot (``_load_runtime_config``) и
    синхронизацию шаблонов workspace;
  * инъекцию API-ключей провайдеров из ``SETTINGS.providers`` в runtime-конфиг;
  * применение таймаутов (LLM / exec / max_iterations) к конфигу и окружению;
  * нормализованный доступ к top-level секциям SETTINGS
    (``settings_section`` — работает и для dict, и для объекта с атрибутами).

Модуль импортируется БЕЗ nanobot: тяжёлые зависимости (nanobot, config)
подключаются лениво внутри методов.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lib.utils.node_access import get_path as _get


class ConfigService:
    """Загрузка и нормализация конфигурации проекта."""

    def __init__(
        self,
        script_dir: Path | None = None,
        workspace_dir: Path | None = None,
    ) -> None:
        self.script_dir = Path(script_dir) if script_dir else None
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None

    # ------------------------------------------------------------------
    # SETTINGS (глобал из config.py)
    # ------------------------------------------------------------------

    @property
    def settings(self) -> Any:
        """Глобальные SETTINGS (project.json + config.json + .secrets.env)."""
        from config import SETTINGS

        return SETTINGS

    def settings_section(self, name: str, default: dict | None = None) -> dict:
        """Вернуть top-level секцию SETTINGS как dict (пусто, если нет).

        SETTINGS может быть как dict-ом, так и объектом с атрибутами
        (в зависимости от формата config.json/.env). Нормализует любой
        вариант к dict — делегирует ``lib.utils.node_access.get_settings_section``
        (единая точка вместе с ``ChannelFactory``).
        """
        from lib.utils.node_access import get_settings_section

        return get_settings_section(self.settings, name, default=default)

    def get_int(self, *path: str, default: int = 0) -> int:
        """Достать int из вложенного dict/AttrDict по цепочке ``path``.

        Если на любом уровне атрибут отсутствует или не приводится к
        ``int`` — возвращает ``default``. Безопасный аксессор для
        ``SETTINGS.gateway.*``, ``SETTINGS.cli.*`` и аналогичных секций.
        """
        val = _get(self.settings, *path, default=None)
        try:
            return int(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    def get_str(self, *path: str, default: str = "") -> str:
        """Достать str из вложенного dict/AttrDict по цепочке ``path``.

        Пустая строка и ``None`` считаются отсутствием → возвращается
        ``default``. Используется для ``SETTINGS.gateway.storage`` и т.п.
        """
        val = _get(self.settings, *path, default=None)
        return str(val) if val else default

    # ------------------------------------------------------------------
    # Runtime-конфиг nanobot
    # ------------------------------------------------------------------

    def load(
        self,
        script_dir: Path | None = None,
        workspace_dir: Path | None = None,
        *,
        sync_templates: bool = True,
    ) -> Any:
        """Загрузить и собрать финальный runtime-конфиг nanobot.

        Args:
            script_dir: корень проекта (где лежит config.json).
            workspace_dir: корень workspace.
            sync_templates: синхронизировать шаблоны workspace при загрузке.

        Returns:
            Runtime-конфиг nanobot (объект с ``providers``, ``channels`` и т.д.).

        Порядок резолва ``${VAR}``:
          1. config.py уже резолвит ``${...}`` в SETTINGS на старте импорта
             (``_resolve_env_refs`` — из ``os.environ``, неизвестный ключ остаётся
             как есть) и доэкспортирует плоские значения в ``os.environ``.
          2. Здесь, до ``_load_runtime_config``, ``_pre_resolve_env_refs``
             подставляет ``*_API_KEY`` из ``SETTINGS.providers``, которых ещё нет
             в ``os.environ`` (секреты из .secrets.env), чтобы nanobot увидел их.
          3. nanobot ``_load_runtime_config`` резолвит оставшиеся ``${VAR}`` из
             ``os.environ``.
        """
        self._pre_resolve_env_refs(script_dir=script_dir)

        from nanobot.cli.commands import _load_runtime_config
        from nanobot.utils.helpers import sync_workspace_templates

        script = Path(script_dir) if script_dir else (self.script_dir or Path.cwd())
        workspace = (
            Path(workspace_dir) if workspace_dir else (self.workspace_dir or script)
        )

        config = _load_runtime_config(
            config=str(script / "config.json"), workspace=str(workspace)
        )
        if sync_templates:
            sync_workspace_templates(config.workspace_path)

        self.apply_provider_keys(config)
        return config

    def _pre_resolve_env_refs(self, script_dir: Path | None) -> None:
        """Pre-resolve ``${VAR}`` placeholders in config.json from SETTINGS.

        nanobot's ``_load_runtime_config`` resolves ``${VAR}`` от ``os.environ``.
        Если в config.json есть ``"apiKey": "${LLM_API_KEY}"``, а в env этого
        нет (ключ задан в ``.secrets.env`` через провайдер-скоупинг,
        ``config.py`` автоматически выставляет ``LLM_API_KEY`` из
        ``SETTINGS.providers.<name>.api_key`` — но только если ключ задан
        скаляром, не плейсхолдером), мы достаём ключ из
        ``SETTINGS.providers.<lower>.api_key`` и временно кладём в ``os.environ``.
        """
        import json as _json

        script = Path(script_dir) if script_dir else (self.script_dir or Path.cwd())
        config_path = script / "config.json"
        if not config_path.exists():
            return

        # Тот же ${VAR}-паттерн, что и config.ENV_REF_PATTERN (единый источник).
        from config import ENV_REF_PATTERN

        pattern = ENV_REF_PATTERN
        try:
            data = _json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return

        missing = set()
        def _walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
            elif isinstance(node, str):
                for m in pattern.finditer(node):
                    var = m.group(1)
                    if var not in os.environ:
                        missing.add(var)
        _walk(data)

        if not missing:
            return

        # Достаём ключи провайдеров из SETTINGS.providers.<name>.api_key
        # (туда config.py уже положил то, что задано в .secrets.env через
        # "# providers: <name>\napi_key=...").
        try:
            settings = self.settings
        except Exception:
            settings = None
        if settings is None:
            return

        for var in missing:
            parts = var.split("_", 1)
            if len(parts) != 2 or parts[1] != "API_KEY":
                continue  # нас интересуют только *_API_KEY плейсхолдеры
            provider_name = parts[0].lower()
            # Канонический LLM_API_KEY — единый ключ для всех провайдеров.
            # Берём из любой непустой секции providers.*.
            if var == "LLM_API_KEY":
                key = ""
                providers = _get(settings, "providers", default={}) or {}
                for prov_cfg in providers.values():
                    if not isinstance(prov_cfg, dict):
                        continue
                    candidate = prov_cfg.get("api_key") or prov_cfg.get("apiKey")
                    if candidate and isinstance(candidate, str) and not candidate.startswith("${"):
                        key = candidate
                        break
            else:
                key = _get(settings, "providers", provider_name, "api_key")
                if not key or not isinstance(key, str):
                    key = _get(settings, "providers", provider_name, "apiKey")
            if not key or not isinstance(key, str):
                continue
            # Если ключ сам — плейсхолдер, пропускаем
            if key.startswith("${"):
                continue
            os.environ[var] = key  # noqa: F821 (set if missing)

    # ------------------------------------------------------------------
    # Ключи провайдеров и таймауты
    # ------------------------------------------------------------------

    def apply_provider_keys(self, config: Any) -> None:
        """Подставить api_key провайдеров из SETTINGS.providers в runtime-конфиг."""
        settings = self.settings
        if not hasattr(settings, "providers"):
            return
        for prov_name, prov_cfg in settings.providers.items():
            api_key = prov_cfg.get("api_key") if hasattr(prov_cfg, "get") else None
            if not api_key:
                continue
            section = getattr(config.providers, prov_name, None)
            if section is not None:
                section.api_key = api_key

    def apply_timeouts(
        self,
        config: Any,
        *,
        llm_timeout: int | None = -1,
        exec_timeout: int | None = -1,
        max_iterations: int | None = None,
    ) -> None:
        """Применить таймауты к конфигу и переменным окружения.

        ``llm_timeout >= 0`` → ``NANOBOT_LLM_TIMEOUT_S`` в os.environ.
        ``exec_timeout >= 0`` → ``config.tools.exec.timeout``.
        ``max_iterations > 0`` → ``config.agents.defaults.max_tool_iterations``.
        """
        if llm_timeout is not None and llm_timeout >= 0:
            os.environ["NANOBOT_LLM_TIMEOUT_S"] = str(llm_timeout)
        if exec_timeout is not None and exec_timeout >= 0:
            try:
                config.tools.exec.timeout = exec_timeout
            except Exception:
                pass
        if max_iterations is not None and max_iterations > 0:
            try:
                config.agents.defaults.max_tool_iterations = max_iterations
            except Exception:
                pass
