"""Что нужно корпусу при старте — независимо от того, кто его поднял.

Точек входа две, и они равноправные:

* собственный сервер (`backend/main.py`) — когда Follow Up работает своим
  интерфейсом;
* MCP-сервер (`backend/skill/mcp_server.py`) — когда его запускает нанобот
  и весь разговор идёт через общий чат.

Раньше вся эта последовательность жила внутри lifespan сервера, и у второй
точки входа её просто не было: MCP-процесс поднимал инструменты, но не
корпус. Читал он то, что лежало на диске, и не пополнялся никогда — а
заметить это можно было только по тому, что свежих актов в ответах нет.

Отсюда модуль. Дублировать последовательность в двух местах нельзя: она
про порядок (владелец файла → целостность → писатели), и разъехавшийся
порядок означает либо две программы над одной базой, либо запись поверх
битого файла.
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)


def open_local_store(*, claim_owner: bool = True) -> Tuple[bool, str]:
    """Поднять локальное хранилище и заявить права на файл базы.

    Возвращает `(ок, причина)`. `False` — файлом владеет другой живой
    процесс: SQLite открыта с `vfs=unix-none`, блокировок нет вовсе, и
    второй писатель портит базу, а не теряет запись.

    `claim_owner=False` — для процессов, которые заведомо только читают.
    """
    from backend.config import get_settings
    from backend.storage import writer
    from backend.storage.database import init_db

    init_db()

    owned = True
    if claim_owner:
        owned = writer.claim_db_owner()

    # Ходы, оставшиеся открытыми после гашения процесса. Без реапера метрики
    # «вызовов на ход» считались бы по грязным данным.
    try:
        from backend.core.memory import state as _mem
        n = _mem.reap_abandoned()
        if n:
            logger.info(f"[boot] Закрыто брошенных ходов: {n}")
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[boot] Реапер ходов: {e}")

    if get_settings().db_startup_integrity_check:
        rep = writer.startup_integrity_check()
        if not rep.ok:
            logger.error(f"[boot] Целостность БД: {rep.detail}")

    if not owned:
        return False, "файлом базы владеет другой живой процесс на этой машине"
    return True, ""


def adopt_agent_llm() -> dict:
    """Взять модель у агента, если нас запустил нанобот.

    Вызывается до первого обращения к модели. Своей модели у навыка в этой
    экосистеме нет: администратор меняет её в конфиге агента, и за ней идут
    все навыки разом.
    """
    from backend.config import get_settings
    from backend.core import nanobot_config

    return nanobot_config.adopt_agent_llm(get_settings())


def adopt_agent_gp() -> dict:
    """Greenplum и схема — у агента, если нас запустил нанобот.

    До первого обращения к GP: иначе синк и гидратация стартуют с пустым
    адресом. Явные значения Follow Up важнее агентских.
    """
    from backend.config import get_settings
    from backend.core import nanobot_config
    from backend.storage import gp

    applied = nanobot_config.adopt_agent_gp(get_settings())
    if applied:
        # Режим доступа мог успеть посчитаться до смены схемы.
        gp._MODE = None
    return applied


def adopt_agent_models() -> dict:
    """Модели — из каталога нанобота, если рядом с нами их нет."""
    from backend.config import get_settings
    from backend.core import nanobot_config

    return nanobot_config.adopt_agent_models(get_settings())


def load_indexes() -> None:
    """Поисковые индексы. Их отсутствие — не повод не стартовать."""
    try:
        from backend.indexing.index_builder import load_bm25, load_faiss
        load_faiss()
        load_bm25()
        logger.info("[boot] FAISS и BM25 загружены")
    except FileNotFoundError:
        logger.warning("[boot] Индексы не найдены — корпус пуст, "
                       "пока не пройдёт гидратация")
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[boot] Индексы не загрузились: {e}")


def start_corpus_upkeep() -> Tuple[bool, str]:
    """Фоновое пополнение корпуса: синк витрины, гидратация, догрузка актов.

    Это то, без чего корпус замерзает. Тот, кто поднял процесс, обязан её
    вызвать — иначе Follow Up отвечает по данным на день установки и молчит
    об этом.

    Возвращает `(запущено, причина отказа)`.
    """
    from backend.config import get_settings
    from backend.storage import gp, writer

    cfg = get_settings()
    if not gp.gp_enabled():
        if cfg.gp_enabled:
            return False, ("GP_ENABLED=true, но psycopg2 не импортировался — "
                           "корпус останется пустым")
        return False, "GP_ENABLED=false — локальный режим"

    try:
        gp.ensure_schema()
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"[boot] Схема GP не применена: {e}")

    allowed, why = writer.background_writers_allowed()
    if not allowed:
        return False, why

    from backend.sync.act_cache_sync import start_background_hydration
    from backend.sync.poruch_sync import start_background_sync

    start_background_sync()
    start_background_hydration()
    # Файлом базы завладел другой процесс (общий NFS, пауза дольше отметки
    # владельца) — писать дальше значит испортить базу.
    from backend.sync.act_cache_sync import stop_background_hydration
    from backend.sync.poruch_sync import stop_background_sync
    writer.on_ownership_lost(stop_background_sync)
    writer.on_ownership_lost(stop_background_hydration)
    if cfg.fu_backfill_enabled:
        from backend.sync.act_backfill import start_background_backfill
        start_background_backfill()
        from backend.sync.act_backfill import stop_background_backfill
        writer.on_ownership_lost(stop_background_backfill)
    return True, ""
