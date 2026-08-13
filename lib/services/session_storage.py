"""SessionStorageService — единый выбор и создание хранилища сессий.

Объединяет логику выбора PG/File/auto из gateway.py и cli_agent.py:

  * источники конфигурации (по приоритету переопределения):
      session_manager.json  →  параметр ``pg`` (секция channels.postgres)
  * режим storage: ``auto`` | ``postgres`` | ``file``;
  * при ``configure_db=True`` и наличии DSN — настройка ``utils.db`` и
    экспорт ``DATABASE_URL`` (нужно инструментам/скриптам);
  * ``storage=postgres`` без DSN → ``SessionStorageError``.

Возвращает ``(manager, mode)``:
  * ``mode == "postgres"`` — PGSessionManager;
  * ``mode == "file"`` — ``SessionManager`` (если ``return_file_manager``)
    или ``None`` (вызывающий сам создаст дефолтное хранилище, как CLI).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple


class SessionStorageError(Exception):
    """Хранилище сессий настроено некорректно (например, postgres без DSN)."""


class SessionStorageService:
    """Фабрика SessionManager / PGSessionManager на основе конфигурации."""

    def __init__(self, session_manager_json: Optional[Path] = None) -> None:
        self._sm_json = Path(session_manager_json) if session_manager_json else None

    # ------------------------------------------------------------------
    # Переопределение из session_manager.json (приоритет над конфигом)
    # ------------------------------------------------------------------

    def _load_override(self) -> dict:
        """Прочитать ``session_manager.json`` (если есть) для override.

        Формат файла — плоский dict, например::

            {"dsn": "postgresql://...", "schema": "audit", "max_conn": 8}

        Поля, заданные здесь, ПЕРЕБИВАЮТ ``pg`` параметр и ``SETTINGS``.
        При отсутствии файла, невалидном JSON или любой ошибке чтения
        возвращается ``{}`` (graceful fallback).
        """
        if self._sm_json is None or not self._sm_json.exists():
            return {}
        try:
            data = json.loads(self._sm_json.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def create(
        self,
        config: Any,
        *,
        storage: str = "auto",
        pg: Optional[dict] = None,
        configure_db: bool = True,
        workspace_dir: Optional[Path] = None,
        return_file_manager: bool = False,
    ) -> Tuple[str, Optional[Any]]:
        """Создать SessionManager подходящего типа.

        Алгоритм:
          1. Мержим ``pg`` (секция channels.postgres из SETTINGS) с
             override из ``session_manager.json`` (последний ПЕРЕБИВАЕТ);
          2. Достаём ``dsn`` из мердженной конфигурации;
          3. Если DSN есть И ``configure_db=True`` — настраиваем
             ``utils.db`` (общий пул для инструментов) и экспортируем
             ``DATABASE_URL`` (нужно для ``tools.exec.allowedEnvKeys``);
          4. Решаем режим ``use_postgres``:
              * ``storage == "postgres"`` — принудительно PG (ошибка
                если DSN не задан);
              * ``storage == "auto"`` — PG если есть DSN, иначе file;
              * ``storage == "file"`` — всегда file.
          5. Возвращаем ``(mode, manager)``.

        Args:
            config: runtime-конфиг nanobot (нужен ``workspace_path``).
            storage: ``"auto"`` | ``"postgres"`` | ``"file"``.
            pg: секция ``channels.postgres`` (dsn, schema, ...).
            configure_db: настраивать ``utils.db`` и ``DATABASE_URL`` при DSN.
            workspace_dir: переопределить workspace (по умолчанию из config).
            return_file_manager: для ``mode="file"`` вернуть
                ``SessionManager(workspace)`` (True) или ``None``
                (False — вызывающий сам создаст дефолтное хранилище,
                как CLI-режим).

        Returns:
            ``(mode, manager)``:
              * ``mode == "postgres"`` — manager = ``PGSessionManager``;
              * ``mode == "file"`` — manager = ``SessionManager``
                (если ``return_file_manager=True``) или ``None``.

        Raises:
            SessionStorageError: ``storage="postgres"`` без DSN.
        """
        pg_cfg = dict(pg or {})
        pg_cfg.update(self._load_override())  # session_manager.json побеждает
        pool_cfg = pg_cfg.get("pool", {}) if isinstance(pg_cfg.get("pool"), dict) else {}
        # Legacy: плоские ключи min_conn/max_conn/pool_timeout в session_manager.json
        # (использовались до введения channels.postgres.pool).
        for legacy_key in ("min_conn", "max_conn", "pool_timeout"):
            if legacy_key in pg_cfg and legacy_key not in pool_cfg:
                pool_cfg[legacy_key] = pg_cfg[legacy_key]

        dsn = pg_cfg.get("dsn") or ""
        workspace = Path(workspace_dir) if workspace_dir else config.workspace_path

        if dsn and configure_db:
            from utils.db import configure

            configure(dsn)
            os.environ["DATABASE_URL"] = dsn

        use_postgres = storage == "postgres" or (
            storage == "auto" and bool(dsn)
        )
        if use_postgres:
            if not dsn:
                raise SessionStorageError(
                    "storage=postgres but no PostgreSQL DSN in config"
                )
            from lib.session.pg_session_manager import PGSessionManager

            manager = PGSessionManager(
                workspace=workspace,
                dsn=dsn,
                schema=pg_cfg.get("schema", "public"),
                messages_table=pg_cfg.get("messages_table", "session_messages"),
                meta_table=pg_cfg.get("meta_table", "session_meta"),
                min_conn=int(pool_cfg.get("min_conn", 1)),
                max_conn=int(pool_cfg.get("max_conn", 4)),
                pool_timeout=float(pool_cfg.get("pool_timeout", 5.0)),
            )
            return "postgres", manager

        if return_file_manager:
            from nanobot.session.manager import SessionManager

            return "file", SessionManager(workspace)
        return "file", None
