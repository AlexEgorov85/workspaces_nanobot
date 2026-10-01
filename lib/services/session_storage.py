"""SessionStorageService — единый выбор и создание хранилища сессий.

Объединяет логику выбора PG/File/auto из gateway.py и cli_agent.py:

  * источник конфигурации — параметр ``pg`` (уже разрешённая секция
    channels.postgres от ConfigurationResolver). ``session_manager.json``
    **не читается здесь** — это делает Resolver (см. ``config.py``).
  * режим storage: ``auto`` | ``postgres`` | ``file``;
  * при ``configure_db=True`` и наличии DSN — настройка ``utils.db`` и
    экспорт ``DATABASE_URL`` (нужно инструментам/скриптам);
  * ``storage=postgres`` без DSN → ``SessionStorageError``;
  * любой созданный менеджер получает ``install_async_save`` — обёртку,
    выносящую синхронный ``save`` из event-loop в executor (6.3).

Возвращает ``(manager, mode)``:
  * ``mode == "postgres"`` — PGSessionManager;
  * ``mode == "file"`` — ``SessionManager`` (если ``return_file_manager``)
    или ``None`` (вызывающий сам создаст дефолтное хранилище, как CLI).
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger


class SessionStorageError(Exception):
    """Хранилище сессий настроено некорректно (например, postgres без DSN)."""


def install_async_save(manager: Any) -> Any:
    """Вынести синхронный ``manager.save`` из event-loop в executor.

    Нативная замена патчу ``RuntimePatcher.patch_async_save`` (change
    ``enterprise-mcp-platform``, фаза 6, п. 6.3). ``nanobot.agent.loop``
    вызывает ``self.sessions.save(...)`` синхронно из async-методов; пока
    save ждёт в очереди пула БД, event loop заморожен, и async-транзакции
    канала (poll/flush/lease) не могут завершиться — возникает взаимная
    блокировка.

    Обёртка ставится **здесь**, при создании хранилища: это единственная
    точка, где агент выбирает менеджер, поэтому ни патч, ни правка
    приватного метода фреймворка не нужны.

      * из потока event loop — реальное сохранение уходит в единый
        последовательный executor (снимок сессии фиксируется на момент
        вызова), вызывающий код возвращается сразу; порядок сохранений
        гарантирован очередью executor'а; ошибки логируются;
      * из остальных потоков (``flush_all``, shutdown, REST-хендлеры) —
        исполняется синхронно, как раньше.

    Args:
        manager: экземпляр SessionManager (или ``None`` — no-op).

    Returns:
        Тот же объект (удобно для ``manager = install_async_save(manager)``).
    """
    if manager is None:
        return manager
    original = getattr(manager, "save", None)
    if original is None or getattr(manager, "_async_save_wrapped", False):
        return manager

    try:
        from nanobot.session.manager import Session
    except Exception as exc:
        logger.warning("install_async_save: import failed: {}", exc)
        return manager

    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="session-save",
    )

    def _snapshot(session: Any) -> Any:
        return Session(
            key=session.key,
            messages=list(session.messages),
            created_at=session.created_at,
            updated_at=session.updated_at,
            metadata=dict(session.metadata or {}),
            last_consolidated=session.last_consolidated,
        )

    def _log_save_error(future) -> None:
        exc = future.exception()
        if exc is not None:
            logger.opt(exception=exc).error("Async session save failed")

    def _wrapped_save(session: Any, fsync: bool = False) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # вне loop — синхронный вызов, как раньше
            return original(session, fsync=fsync)
        future = executor.submit(original, _snapshot(session), fsync=fsync)
        future.add_done_callback(_log_save_error)
        return None

    manager.save = _wrapped_save
    manager._async_save_executor = executor
    manager._async_save_wrapped = True
    return manager


class SessionStorageService:
    """Фабрика SessionManager / PGSessionManager на основе конфигурации.

    Замечание: до этого плана этот класс сам читал ``session_manager.json``
    через ``_load_override()`` и применял его к ``pg_cfg`` **после**
    ``SETTINGS`` — это перетирало runtime-таблицы, которые профиль уже
    установил. Теперь override применяется централизованно в
    ``config.resolve_application_config()`` (см. порядок merge в
    ``config.py``: session_manager.json идёт на шаге 2, profile overlay —
    на шаге 4 ПОСЛЕДНИМ).
    """

    def create(
        self,
        config: Any,
        *,
        storage: str = "auto",
        pg: dict | None = None,
        configure_db: bool = True,
        workspace_dir: Path | None = None,
        return_file_manager: bool = False,
    ) -> tuple[str, Any | None]:
        """Создать SessionManager подходящего типа.

        Алгоритм:
          1. Берём уже разрешённый ``pg`` от ConfigurationResolver;
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
            pg: уже разрешённая секция ``channels.postgres`` (dsn, schema, ...).
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
        pool_cfg = pg_cfg.get("pool", {}) if isinstance(pg_cfg.get("pool"), dict) else {}
        # Legacy: плоские ключи min_conn/max_conn/pool_timeout (использовались
        # до введения channels.postgres.pool — теперь поставляются через
        # Resolver как часть pg_cfg).
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

            messages_table = pg_cfg.get("messages_table", "")
            meta_table = pg_cfg.get("meta_table", "")
            if not messages_table or not meta_table:
                raise SessionStorageError(
                    "storage=postgres: channels.postgres.messages_table и "
                    "channels.postgres.meta_table обязательны "
                    "(нет авто-дефолтов в коде). "
                    f"messages_table={messages_table!r}, meta_table={meta_table!r}"
                )
            manager = PGSessionManager(
                workspace=workspace,
                dsn=dsn,
                schema=pg_cfg.get("schema", "public"),
                messages_table=messages_table,
                meta_table=meta_table,
                min_conn=int(pool_cfg.get("min_conn", 1)),
                max_conn=int(pool_cfg.get("max_conn", 4)),
                pool_timeout=float(pool_cfg.get("pool_timeout", 5.0)),
            )
            return "postgres", install_async_save(manager)

        if return_file_manager:
            from nanobot.session.manager import SessionManager

            return "file", install_async_save(SessionManager(workspace))
        return "file", None
