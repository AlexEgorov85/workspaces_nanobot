"""PreloadService — фоновые предзагрузки данных при старте агента.

Объединяет ДВА несвязанных механизма из gateway.py и cli_agent.py:

  1. ``preload_vector_indexes(store)`` — прогрев FAISS-индексов из
     DuckDB-кэша в память (gateway, AuditMemoryStore);
  2. ``preload_audit_cache(config)`` / ``background_audit_cache_refresh`` —
     загрузка файла кеша навыка audit_analyzer из PostgreSQL и его
     периодическое обновление (cli_agent).

Настройки кеша навыка читаются из ``settings.skills.audit_analyzer``
(если settings не передан — из глобальных ``config.SETTINGS``).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional, Tuple


def _get(node: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        try:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = getattr(node, key)
        except (AttributeError, KeyError, TypeError):
            return default
        if node is None:
            return default
    return node


class PreloadService:
    """Фоновые предзагрузки кешей/индексов.

    Этот сервис НЕ знает о цикле запуска — он предоставляет async-методы,
    которые точки входа (``gateway.py`` / ``cli_agent.py``) запускают
    как ``asyncio.create_task`` и останавливают через ``stop_tasks``.

    Attributes:
        _settings: ``SETTINGS`` (для чтения ``skills.audit_analyzer``).
            Если не передан — читается глобальный ``config.SETTINGS``
            при первом обращении.
    """

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # Кеш навыка audit_analyzer
    # ------------------------------------------------------------------

    @property
    def _audit_settings(self) -> Any:
        """Секция ``SETTINGS.skills.audit_analyzer`` (lazy).

        Если ``settings`` не передан в конструктор — читает глобальный
        ``config.SETTINGS``. Исключения глотаются и дают ``{}``
        (навык просто не будет предзагружаться).
        """
        if self._settings is not None:
            return _get(self._settings, "skills", "audit_analyzer", default={})
        try:
            from config import SETTINGS

            return _get(SETTINGS, "skills", "audit_analyzer", default={})
        except Exception:
            return {}

    def get_audit_cache_config(self, config: Any) -> Tuple[Optional[str], Optional[dict]]:
        """Вернуть ``(cache_file_path, db_config)`` для DuckDB-кеша навыка.

        Если ``in_memory_enabled != True`` или ``in_memory_cache_path``
        не задан — возвращает ``(None, None)`` (навык без in-memory
        кеша; CLI тогда работает напрямую через PG).

        Относительный путь резолвится относительно
        ``config.workspace_path / "skills" / "audit_analyzer"`` —
        стандартное место для навыка. Абсолютный путь используется
        как есть.

        Returns:
            ``(cache_file_path, db_config)`` или ``(None, None)``.

        Все исключения глотаются → ``(None, None)`` (graceful
        fallback: ошибка чтения конфига не должна ломать старт).
        """
        try:
            acfg = self._audit_settings
            if not _get(acfg, "in_memory_enabled", default=False):
                return None, None
            cache_path = _get(acfg, "in_memory_cache_path", default="")
            if not cache_path:
                return None, None

            cache_file = Path(cache_path)
            if not cache_file.is_absolute():
                cache_file = (
                    config.workspace_path / "skills" / "audit_analyzer" / cache_file
                )

            from skills.audit_analyzer.scripts.skill_config import load_db_config

            return str(cache_file), load_db_config()
        except Exception:
            return None, None

    def preload_audit_cache(self, config: Any) -> None:
        """Подгрузить DuckDB-кеш навыка, если он устарел (> 1 часа) или отсутствует.

        Синхронная (блокирующая) функция — вызывается ДО запуска
        ``AgentLoop.run()``, чтобы навык сразу мог читать из кеша.

        Логика:
          * если кеш отсутствует → загрузить;
          * если кешу < 1 часа → не трогать (свежий);
          * если кешу ≥ 1 часа → перезагрузить.

        Ошибка загрузки логируется как WARNING (не критично —
        навык может работать без кеша, просто медленнее).
        """
        cache_path, db_cfg = self.get_audit_cache_config(config)
        if not cache_path or not db_cfg:
            return

        from lib.services.cache_provider_impl import load_cache_from_postgres

        cache_file = Path(cache_path)
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 3600:
                return

        try:
            load_cache_from_postgres(cache_path, db_cfg)
        except Exception as exc:
            import logging

            logging.getLogger("preload").warning(
                "audit_analyzer cache preload failed (non-critical): %s", exc
            )

    async def background_audit_cache_refresh(self, config: Any) -> None:
        """Бесконечная фоновая задача: каждый час проверять свежесть кеша.

        Использует ``cache_provider_impl.check_cache_stale`` (сравнивает
        ``MAX(updated_at)`` в PG с текущим состоянием кеша). Если хотя
        бы одна таблица устарела — полная перезагрузка.

        Завершается через ``asyncio.CancelledError`` (при ``stop_tasks``).
        Не выходит сам — это long-running task, рассчитанная на время
        жизни процесса.
        """
        cache_path, db_cfg = self.get_audit_cache_config(config)
        if not cache_path or not db_cfg:
            return

        from lib.services.cache_provider_impl import (
            check_cache_stale,
            load_cache_from_postgres,
        )
        import logging

        while True:
            try:
                await asyncio.sleep(3600)
                result = check_cache_stale(cache_path, db_cfg)
                if result.get("stale_tables"):
                    logging.getLogger("cache").info(
                        "Audit cache stale tables: %s, reloading...",
                        result["stale_tables"],
                    )
                    load_cache_from_postgres(cache_path, db_cfg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.getLogger("cache").warning(
                    "Audit cache refresh check failed: %s", exc
                )

    # ------------------------------------------------------------------
    # FAISS-индексы (gateway)
    # ------------------------------------------------------------------

    async def preload_vector_indexes(self, store: Any) -> Optional[list]:
        """Прогреть FAISS-индексы из DuckDB-кэша в память (gateway).

        ``store.preload_indexes()`` — тяжёлая синхронная операция
        (читает из DuckDB, строит FAISS-индексы для каждого источника
        в ``vector_db_table``). Запускаем в ``asyncio.to_thread``, чтобы
        не блокировать event loop.

        Вызывающий (обычно ``gateway.py:_run``) ОБЯЗАН дождаться
        ``AuditSyncService`` initial_load ПЕРЕД этим методом (иначе
        DuckDB пуст и preload вернёт ``[]``). В gateway это решается
        через ``asyncio.Event`` + таймаут 30с.

        Returns:
            Список построенных индексов вида
            ``[{"index_name": "audits_index", "vectors": 10}, ...]``
            или ``None``, если ``store is None`` / ``not is_ready()`` /
            произошло исключение.
        """
        if store is None or not store.is_ready():
            return None
        try:
            return await asyncio.to_thread(store.preload_indexes)
        except Exception:
            return None

    async def start_audit_cache_tasks(self, config: Any) -> list:
        """Создать ``asyncio.Task`` для ``background_audit_cache_refresh``.

        Returns:
            Список из одной задачи (для совместимости с ``stop_tasks``,
            который принимает ``list``).
        """
        task = asyncio.create_task(self.background_audit_cache_refresh(config))
        return [task]

    async def stop_tasks(self, tasks: list) -> None:
        """Отменить фоновые задачи и дождаться их завершения.

        ``asyncio.gather(..., return_exceptions=True)`` гасит
        ``CancelledError`` и любые другие исключения внутри задач —
        мы не хотим, чтобы они пропадали в ``warnings``.
        """
        for task in tasks:
            if task and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
