"""
Шлюз (gateway) для nanobot — долгоживущий сервер с каналами связи.

╔══════════════════════════════════════════════════════════════════════════╗
║                          АРХИТЕКТУРА                                   ║
╚══════════════════════════════════════════════════════════════════════════╝

gateway.py — это точка входа для запуска nanobot как долгоживущего сервера.
В отличие от cli_agent.py (терминальный интерфейс, stdin/stdout), gateway
общается с пользователями через внешние каналы связи (channels):

  • Telegram / Slack / webhook  — стандартные каналы из nanobot (ChannelManager)
  • Redis                       — канал поверх Redis pub/sub (incoming/outgoing keys)
  • PostgreSQL                  — канал поверх таблицы conversation_messages
  • Streamlit UI                — локальный веб-интерфейс чата на :8501

Процесс рассчитан на работу "вечно": при необработанном исключении
gateway перезапускается сам с exponential backoff (1с → 2с → ... → 30с).

Основные отличия от cli_agent.py:
  • Нет интерактивного цикла ввода — всё общение идёт через шину сообщений
    (MessageBus) и каналы, которые читают входящие сообщения и публикуют ответы.
  • Сессии по умолчанию хранятся в PostgreSQL (PGSessionManager), если
    настроен DSN; иначе — в JSONL-файлах (SessionManager).
  • Встроенный ToolAuditHook — аудит вызовов инструментов в metadata ответа.
  • Monkey-patch ContextGovernor.normalize_tool_result — большие результаты
    инструментов выгружаются в data_store/ (файл вместо гигантской строки).


╔══════════════════════════════════════════════════════════════════════════╗
║                          ЖИЗНЕННЫЙ ЦИКЛ                                ║
╚══════════════════════════════════════════════════════════════════════════╝

  1. Загрузка конфига
     Читается config.json из той же директории, что и gateway.py.
     Запускается sync_workspace_templates() — синхронизация шаблонов workspace.

  1b. Подстановка API-ключей
     Из SETTINGS.providers (секция в .env / config.json) ключи провайдеров
     подставляются в config.providers.*.api_key.

  2. Создание шины сообщений (MessageBus)
     Центральная шина: агент и каналы обмениваются через неё.
     Inbound  → канал → агент
     Outbound → агент → канал

  3. Выбор хранилища сессий
     • storage=postgres → принудительно PGSessionManager (требует DSN)
     • storage=file     → JSONL-файлы (SessionManager)
     • storage=auto     → PG если есть dsn в конфиге, иначе JSONL

  4. Monkey-patch ContextGovernor.normalize_tool_result
     Результаты инструментов больше persist_threshold байт сохраняются
     в workspace/data_store/ как файлы; в контекст LLM подставляется
     короткая ссылка "[Result saved to data_store/...]" (защита от
     раздувания контекста). read_file исключён из выгрузки во избежание
     циклов persist → read → persist.

  5. Таймауты и логирование
     LLM-таймаут (NANOBOT_LLM_TIMEOUT_S) и exec-таймаут из настроек.
     Уровень логов loguru настраивается из SETTINGS.gateway.log_level.

  6. Создание AgentLoop
     Главный цикл агента. Подключается ToolAuditHook и monkey-patch
     _assemble_outbound, внедряющий аудит тулов в result.metadata.

  7. ChannelManager + Redis/Postgres каналы
     ChannelManager управляет стандартными каналами nanobot. Redis и
     Postgres каналы создаются и регистрируются вручную (по настройкам
     SETTINGS.channels.redis / SETTINGS.channels.postgres).

  8. Сервисы синхронизации audit_analyzer
     Gateway — владелец файла кеша навыка. AuditSyncService (единственный
     владелец подключения к PostgreSQL, worker-поток) инкрементально
     синхронизирует таблицы в AuditMemoryStore (in-memory DuckDB + FAISS),
     а после каждого цикла публикует атомарный снимок (temp + os.replace)
     в файл кеша навыка (in_memory_cache_path). Навык (CLI) только читает
     этот файл — создание и обновление его больше не касаются:
     • _build_audit_services()    — создание (store, sync_service)
     • sync_service.start(True)   — initial load + поллинг по track-колонке
     • store.publish()            — снимок для CLI (по on_sync_callback)
     • _preload_vector_indexes()  — прогрев FAISS-индексов в память

  9. Запуск (run())
     Стартует все каналы, поднимает Streamlit UI (:8501) как subprocess,
     запускает AgentLoop. При завершении корректно останавливает каналы,
     закрывает MCP, сбрасывает сессии на диск.

  10. Перезапуск с backoff
      Если run() упал с исключением — пауза restart_delay, удвоение
      задержки до max_restart_delay, затем повторный запуск.
      Чистое завершение (clean shutdown) выходит из цикла.


╔══════════════════════════════════════════════════════════════════════════╗
║                     СТРУКТУРА ФАЙЛА                                    ║
╚══════════════════════════════════════════════════════════════════════════╝

  1. Импорты и пути
     ─────────────────────
     _SCRIPT_DIR   — директория со скриптом
     _WORKSPACE_DIR— корень workspace (tools/, hooks/, data_store/)

  2. Вспомогательные функции
     ─────────────────────────
     _settings_section()          — достать top-level секцию SETTINGS как dict
     _resolve_transcription_key() — API-ключ провайдера транскрипции (openai/groq)
     _resolve_transcription_base()— базовый URL API транскрипции

  3. main() — запуск шлюза (все этапы по номерам выше)

  4. Точка входа
     ─────────────
     if __name__ == "__main__": main()


╔══════════════════════════════════════════════════════════════════════════╗
║                         НАСТРОЙКИ                                       ║
╚══════════════════════════════════════════════════════════════════════════╝

  Все настройки — в .env / config.json (секция SETTINGS.gateway):

    storage                  — "postgres" | "file" | "auto"
    persist_threshold        — мин. размер результата (байт) для выгрузки в data_store
    persist_max_files        — макс. файлов в data_store
    persist_max_age_hours    — макс. возраст файла (часы)
    llm_timeout              — таймаут LLM вызова, сек (>=0 активирует env var)
    exec_timeout             — таймаут exec-скриптов, сек
    log_level                — уровень логов loguru (например "WARNING")

  Каналы (SETTINGS.channels):
    redis.enabled            — включить Redis-канал
    postgres.enabled         — включить Postgres-канал
    postgres.dsn             — строка подключения к БД
    transcription_provider   — "openai" | "groq" (транскрипция голосовых)


╔══════════════════════════════════════════════════════════════════════════╗
║                         ПРИМЕРЫ ЗАПУСКА                                ║
╚══════════════════════════════════════════════════════════════════════════╝

  # Запуск gateway со стандартными настройками
  python gateway.py

  # С логированием в stderr на уровне DEBUG
  # (SETTINGS.gateway.log_level = "DEBUG" в config.json)
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
import traceback
from pathlib import Path

# ── Пути скрипта и workspace ────────────────────────────────────────────────
# _SCRIPT_DIR    — директория, где лежит gateway.py (корень проекта nanobot)
# _WORKSPACE_DIR — корень workspace (tools/, hooks/, data_store/, skills/).
# Оба добавляются в sys.path, чтобы импорты вида `from utils...`, `from hooks...`
# и `from config import SETTINGS` работали независимо от рабочего каталога.
_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))

import json

from loguru import logger
from lib.channels.postgres_channel import PostgresChannel
from lib.channels.redis_channel import RedisChannel
from utils.session_file_store import SessionFileStore, prepare_content

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.cli.commands import _load_runtime_config, console, __logo__, __version__
from lib.session.pg_session_manager import PGSessionManager
from nanobot.utils.helpers import sync_workspace_templates

from hooks.tool_audit_hook import ToolAuditHook

from config import SETTINGS


def _settings_section(name: str, default: dict | None = None) -> dict:
    """Вернуть top-level секцию SETTINGS как dict (пусто, если нет).

    SETTINGS может быть как dict-ом, так и объектом с атрибутами
    (в зависимости от формата config.json/.env). Функция нормализует
    любой из вариантов к dict.

    Пример: _settings_section("channels").get("redis") — конфиг Redis-канала.
    """
    if isinstance(SETTINGS, dict):
        node = SETTINGS.get(name, {}) or {}
    else:
        node = getattr(SETTINGS, name, {}) or {}
    return node if isinstance(node, dict) else (default or {})


def _resolve_transcription_key(config):
    """Вернуть API-ключ для провайдера транскрипции.

    Поддерживает openai и groq. Если ключ не найден — пустая строка.
    Используется для распознавания голосовых сообщений в Postgres-канале.
    """
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_key
        return config.providers.groq.api_key
    except AttributeError:
        return ""


def _resolve_transcription_base(config):
    """Вернуть базовый URL для API транскрипции.

    Поддерживает openai и groq. Если не задан — пустая строка
    (тогда используется стандартный endpoint провайдера).
    """
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_base or ""
        return config.providers.groq.api_base or ""
    except AttributeError:
        return ""


def main() -> None:
    """Запускает gateway со всеми локальными доработками."""

    # ── 1. Загрузка конфигурации ─────────────────────────────────────────
    # Читаем config.json из директории скрипта, резолвим переменные
    # окружения, синхронизируем шаблоны workspace (новые файлы/инструменты).
    config = _load_runtime_config(config=str(_SCRIPT_DIR / "config.json"), workspace=str(_WORKSPACE_DIR))
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot gateway v{__version__}...")

    # ── 1b. Подстановка API-ключей провайдеров из .secrets.env ────────────
    # SETTINGS.providers содержит api_key для каждого провайдера
    # (обычно из .secrets.env). Перекладываем их в загруженный config,
    # чтобы агент и инструменты использовали актуальные ключи.
    if hasattr(SETTINGS, "providers"):
        for prov_name, prov_cfg in SETTINGS.providers.items():
            api_key = prov_cfg.get("api_key") if hasattr(prov_cfg, "get") else None
            if api_key:
                section = getattr(config.providers, prov_name, None)
                if section is not None:
                    section.api_key = api_key

    # ── 2. Шина сообщений и SessionManager ───────────────────────────────
    # MessageBus — центральная шина: каналы публикуют inbound, агент
    # публикует outbound. SessionManager хранит историю сессий.
    bus = MessageBus()

    # Конфиг Postgres для хранилища сессий (секция channels.postgres).
    # Если задан DSN — настраиваем utils.db (configure) и экспортируем
    # DATABASE_URL в окружение (нужно некоторым инструментам/скриптам).
    pg = _settings_section("channels").get("postgres", {})
    dsn = pg.get("dsn", "")
    if dsn:
        from utils.db import configure
        configure(dsn)
        import os
        os.environ["DATABASE_URL"] = dsn

    # Выбор хранилища сессий:
    #   postgres → принудительно PGSessionManager (нужен DSN, иначе выходим)
    #   auto     → PG если есть DSN, иначе JSONL-файлы
    #   file     → JSONL-файлы (ветка else ниже)
    use_postgres = SETTINGS.gateway.storage == "postgres" or (SETTINGS.gateway.storage == "auto" and bool(dsn))
    if use_postgres:
        if not dsn:
            console.print("[red]✗[/red] storage=postgres but pg.dsn is empty in config")
            sys.exit(1)
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
            schema=pg.get("schema", "public"),
            messages_table=pg.get("messages_table", "session_messages"),
            meta_table=pg.get("meta_table", "session_meta"),
        )
        console.print("[green]✓[/green] PGSessionManager: sessions stored in PostgreSQL")
    else:
        from nanobot.session.manager import SessionManager
        session_manager = SessionManager(config.workspace_path)
        if dsn:
            console.print("[dim]PostgreSQL DSN available but storage=file; using JSONL files[/dim]")
        else:
            console.print("[yellow]⚠[/yellow] No PostgreSQL DSN — using JSONL files")

    # ── 3. Monkey-patch ContextGovernor.normalize_tool_result ──────────────
    # v0.3.0: AgentRunner._normalize_tool_result перенесён в
    # ContextGovernor.normalize_tool_result (nanobot/agent/context_governance.py).
    #
    # Идея: огромные результаты инструментов (например, вывод скрипта на
    # сотни КБ) раздувают контекст LLM и дорого стоят. Поэтому результаты
    # больше persist_threshold байт сохраняются в data_store/ как файлы,
    # а в контекст подставляется короткая ссылка на файл.
    if SETTINGS.gateway.persist_threshold > 0:
        # Файловое хранилище для "выгруженных" результатов инструментов.
        # max_files / max_age_hours ограничивают рост data_store.
        _persisted_store = SessionFileStore(
            _WORKSPACE_DIR / "data_store",
            max_files=SETTINGS.gateway.persist_max_files,
            max_age_hours=SETTINGS.gateway.persist_max_age_hours,
        )
        try:
            from nanobot.agent.context_governance import ContextGovernor
            from nanobot.utils.runtime import ensure_nonempty_tool_result

            # read_file исключён из выгрузки, чтобы избежать циклов
            # persist → прочитал файл → persist прочитанного → ...
            _EXEMPT_TOOLS = frozenset({"read_file"})
            _original = ContextGovernor.normalize_tool_result

            def _normalize_with_persist(config, tool_call_id, tool_name, result):
                # 1. Нормализуем "пустой" результат (пустые строки и т.п.)
                result = ensure_nonempty_tool_result(tool_name, result)
                # 2. Инструменты из _EXEMPT_TOOLS пропускаем без выгрузки
                if tool_name in _EXEMPT_TOOLS:
                    return result

                # 3. Приводим результат к тексту (str напрямую, остальное — JSON)
                text = None
                if isinstance(result, str):
                    text = result
                elif not isinstance(result, bytes):
                    try:
                        text = json.dumps(result, ensure_ascii=False, indent=2)
                    except (TypeError, ValueError):
                        pass

                # 4. Если текст больше порога — сохраняем в data_store/
                #    и подставляем короткую ссылку вместо полного содержимого.
                if text is not None and len(text.encode("utf-8")) > SETTINGS.gateway.persist_threshold:
                    try:
                        content, ext = prepare_content(text)
                        save_info = _persisted_store.save(
                            session_key=config.session_key or "default",
                            content=content,
                            source_tool=tool_name,
                            ext=ext,
                        )
                        return (
                            f"[Result saved to data_store/"
                            f"{save_info['path']} ({save_info['size_kb']} KB)]"
                        )
                    except OSError as _exc:
                        # disk full, permissions, etc — fall back to original
                        # (не выгружаем, возвращаем как есть)
                        pass

                # 5. Иначе — оригинальная нормализация без изменений
                return _original(config, tool_call_id, tool_name, result)

            # staticmethod, т.к. ContextGovernor.normalize_tool_result —
            # статический метод; обёртка должна сохранить эту сигнатуру.
            ContextGovernor.normalize_tool_result = staticmethod(_normalize_with_persist)
            console.print("[green]✓[/green] ContextGovernor.normalize_tool_result patched")
        except Exception as exc:
            # Если патч не применился (версия nanobot изменилась) —
            # не роняем gateway, а просто предупреждаем.
            console.print(f"[yellow]⚠[/yellow] _normalize_tool_result patch failed: {exc}")

    # ── 4. Таймауты ───────────────────────────────────────────────────────
    # LLM-таймаут пробрасывается в окружение (NANOBOT_LLM_TIMEOUT_S),
    # exec-таймаут — в config.tools.exec.timeout.
    import os
    if SETTINGS.gateway.llm_timeout >= 0:
        os.environ["NANOBOT_LLM_TIMEOUT_S"] = str(SETTINGS.gateway.llm_timeout)
    if SETTINGS.gateway.exec_timeout >= 0:
        try:
            config.tools.exec.timeout = SETTINGS.gateway.exec_timeout
        except Exception:
            pass

    # ── 5. Логирование ───────────────────────────────────────────────────
    # Перенастраиваем loguru: убираем дефолтные обработчики и пишем
    # в stderr с заданным уровнем (WARNING подавляет INFO-шум).
    try:
        logger.remove()
        logger.add(sys.stderr, level=SETTINGS.gateway.log_level)
    except Exception:
        pass

    # ── 6. Создание AgentLoop ────────────────────────────────────────────
    # ToolAuditHook собирает события вызовов инструментов за ход агента.
    tool_audit_hook = ToolAuditHook()
    agent = AgentLoop.from_config(
        config, bus,
        session_manager=session_manager,
        hooks=[tool_audit_hook],
    )

    # Monkey-patch _assemble_outbound — внедряем аудит тулов в metadata.
    # _assemble_outbound формирует финальное outbound-сообщение агента.
    # Обёртка добавляет в result.metadata["_tool_audit"] список записей
    # из tool_audit_hook.drain() (вызовы инструментов за этот ход).
    # Это позволяет каналам показать пользователю, какие инструменты
    # вызывались и с каким результатом.
    _orig_assemble = agent._assemble_outbound

    def _assemble_with_audit(msg, final_content, all_msgs, stop_reason, had_injections, on_stream, *, turn_latency_ms=None):
        result = _orig_assemble(msg, final_content, all_msgs, stop_reason, had_injections, on_stream, turn_latency_ms=turn_latency_ms)
        if result is not None:
            entries = tool_audit_hook.drain()
            if entries:
                result.metadata["_tool_audit"] = entries
        return result

    agent._assemble_outbound = _assemble_with_audit
    # ── 7. ChannelManager ────────────────────────────────────────────────
    # ChannelManager управляет стандартными каналами nanobot (Telegram,
    # Slack, webhook и т.д.). Redis/Postgres каналы добавляются ниже вручную.
    channels = ChannelManager(config, bus, session_manager=session_manager)

    # ── 8. Redis-канал ────────────────────────────────────────────────────
    # Канал поверх Redis: читает сообщения из incoming_key, публикует
    # ответы в outgoing_prefix. Настройки — секция SETTINGS.channels.redis.
    rs = _settings_section("channels").get("redis", {})
    if rs.get("enabled", False):
        redis_cfg = {
            "enabled": True,
            "host": rs.get("host", "127.0.0.1"),
            "port": rs.get("port", 6379),
            "db": rs.get("db", 0),
            "password": rs.get("password"),
            "incoming_key": rs.get("incoming_key", "nanobot:inbox"),
            "outgoing_prefix": rs.get("outgoing_prefix", "nanobot:outbox"),
            "poll_timeout": rs.get("poll_timeout", 5.0),
            "max_concurrent": rs.get("max_concurrent", 1),
            "allow_from": rs.get("allow_from", ["*"]),
        }
        redis_channel = RedisChannel(redis_cfg, bus)
        # Пробрасываем глобальные настройки вывода из конфига в канал
        redis_channel.send_progress = config.channels.send_progress
        redis_channel.send_tool_hints = config.channels.send_tool_hints
        redis_channel.show_reasoning = config.channels.show_reasoning
        channels.channels["redis"] = redis_channel
        console.print("[green]✓[/green] Redis channel enabled")
    else:
        console.print("[dim]Redis channel disabled[/dim]")

    # ── 9. Postgres-канал ────────────────────────────────────────────────
    # Канал поверх таблицы conversation_messages в PostgreSQL: агент
    # отвечает, записывая строку в таблицу (интеграция с внешними БП).
    # Плюс транскрипция голосовых через transcription_provider.
    if pg.get("enabled", False):
        if not dsn:
            console.print("[red]✗[/red] PostgresChannel enabled but no DSN (channels.postgres.dsn)")
        else:
            ch_cfg = {
                "enabled": True,
                "dsn": dsn,
                "schema": pg.get("schema", "public"),
                "table_name": pg.get("table_name", "conversation_messages"),
                "poll_interval": pg.get("poll_interval", 2.0),
                "flush_interval": pg.get("flush_interval", 2.0),
                "max_concurrent": pg.get("max_concurrent", 1),
                "processing_timeout": pg.get("processing_timeout", 120),
                "allow_from": pg.get("allow_from", ["*"]),
            }
            pg_channel = PostgresChannel(ch_cfg, bus)
            # Транскрипция голосовых (whisper): провайдер, ключ, базовый URL,
            # язык распознавания — берём из конфига каналов.
            pg_channel.transcription_provider = config.channels.transcription_provider
            pg_channel.transcription_api_key = _resolve_transcription_key(config)
            pg_channel.transcription_api_base = _resolve_transcription_base(config)
            pg_channel.transcription_language = config.channels.transcription_language
            pg_channel.send_progress = config.channels.send_progress
            pg_channel.send_tool_hints = config.channels.send_tool_hints
            pg_channel.show_reasoning = config.channels.show_reasoning
            channels.channels["postgres"] = pg_channel
            console.print("[green]✓[/green] PostgreSQL channel enabled")
    else:
        console.print("[dim]PostgreSQL channel disabled[/dim]")

    console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")

    _streamlit_script = _SCRIPT_DIR / "streamlit_app.py"

    # ── 11. Сервисы синхронизации audit_analyzer ─────────────────────────────
    # Единственное подключение к PostgreSQL живёт в worker-потоке
    # AuditSyncService, который инкрементально передаёт изменения в
    # AuditMemoryStore (DuckDB-кэш + FAISS). Навык не трогаем — его CLI
    # продолжает работать со своим in-memory кэшем.

    def _build_audit_services():
        """Построить (store, sync_service) для audit_analyzer или (None, None).

        Сервисы создаются, если включён in-memory кеш (in_memory_enabled),
        задан DSN и есть таблицы.

        ВАЖНО: gateway — владелец файла кеша навыка. AuditMemoryStore держит
        живое зеркало в чисто in-memory DuckDB (cache_path="") и после каждого
        цикла синхронизации публикует снимок в файл навыка (publish_path =
        in_memory_cache_path) атомарно (temp + os.replace). DuckDB допускает
        только один процесс-писатель на файл, поэтому gateway никогда не
        открывает целевой файл на запись — навык (CLI) читает его на чтение
        в любой момент без конфликтов.
        """
        try:
            acfg = SETTINGS.skills.audit_analyzer
            if not acfg.get("in_memory_enabled", False):
                return None, None
            if not dsn:
                return None, None

            tables = [t for t in (acfg.get("db_tables", []) or []) if t]
            vector_table = acfg.get("mode_vector_db_table", "") or ""
            schema = acfg.get("db_schema", "oarb") or "oarb"
            if not vector_table and not tables:
                return None, None

            # Целевой файл снимка — кеш навыка (in_memory_cache_path)
            publish_path = ""
            cp = acfg.get("in_memory_cache_path", "") or ""
            if cp:
                p = Path(cp)
                publish_path = (
                    str(config.workspace_path / "skills" / "audit_analyzer" / cp)
                    if not p.is_absolute() else str(p)
                )

            from lib.services.audit_memory_store import AuditMemoryStore
            from lib.services.audit_sync_service import AuditSyncService

            store = AuditMemoryStore(
                cache_path="",
                publish_path=publish_path,
                schema=schema,
                tables=tables or None,
                vector_db_table=vector_table,
                embedding_base_url=acfg.get("embedding_base_url", "") or "",
                embedding_model=acfg.get("embedding_model", "mxbai-embed-large:latest"),
            )
            sync_service = AuditSyncService(
                dsn=dsn,
                schema=schema,
                tables=(tables + [vector_table]) if vector_table else tables,
                vector_table=vector_table,
                poll_interval_sec=float(acfg.get("poll_interval_sec", 60)),
                write_table=acfg.get("sync_write_table", "audit_interactions"),
                write_schema=schema,
            )
            return store, sync_service
        except Exception:
            return None, None

    async def _preload_vector_indexes(store):
        """Фоновый прогрев FAISS-индексов из DuckDB-кэша в память при старте."""
        if store is None or not store.is_ready():
            return
        try:
            loaded = await asyncio.to_thread(store.preload_indexes)
            if loaded:
                for item in loaded:
                    console.print(
                        f"[green]✓[/green] vector index '{item['index_name']}' "
                        f"built in memory: {item['vectors']} vectors"
                    )
            else:
                console.print("[dim]audit_analyzer vector indexes: нет данных в кэше[/dim]")
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] audit_analyzer vector index preload failed: {exc}")

    # ── 12. Запуск ───────────────────────────────────────────────────────
    async def run():
        store = None
        sync_service = None

        # Стартуем все каналы (включая Redis/Postgres выше) как фоновую задачу
        channels_task = asyncio.create_task(channels.start_all())

        # Запуск Streamlit UI (веб-интерфейс чата)
        # Поднимаем отдельный subprocess `streamlit run streamlit_app.py` на :8501.
        # Логи пишем в logs/streamlit.log (append).
        _streamlit_proc: subprocess.Popen | None = None
        _streamlit_log_handle = None
        if _streamlit_script.exists():
            try:
                _streamlit_log = _SCRIPT_DIR / "logs" / "streamlit.log"
                _streamlit_log_handle = open(_streamlit_log, "a", encoding="utf-8")
                _streamlit_proc = subprocess.Popen(
                    [sys.executable, "-m", "streamlit", "run", str(_streamlit_script),
                     "--server.headless", "true",
                     "--server.port", "8501"],
                    stdout=_streamlit_log_handle,
                    stderr=subprocess.STDOUT,
                )
                console.print("[green]✓[/green] Streamlit UI started on :8501")
            except Exception as exc:
                # Если Streamlit не поднялся — gateway продолжает работать
                console.print(f"[yellow]⚠[/yellow] Streamlit failed to start: {exc}")

        try:
            # Фоновые сервисы синхронизации audit_analyzer
            store, sync_service = _build_audit_services()
            if store is not None and sync_service is not None:
                store.open()
                sync_service.set_on_new_records_callback(store.upsert_records)
                sync_service.set_on_sync_callback(store.publish)
                sync_service.start(initial_load=True)
                if store.get_stats().get("publish_path"):
                    console.print(
                        f"[green]✓[/green] audit_analyzer sync started "
                        f"(in-memory cache + vectors, публикация кеша навыка: "
                        f"{store.get_stats()['publish_path']})"
                    )
                else:
                    console.print("[green]✓[/green] audit_analyzer sync started (in-memory cache + vectors)")
                asyncio.create_task(_preload_vector_indexes(store))
            else:
                console.print("[dim]audit_analyzer sync disabled (in_memory_enabled/dsn)[/dim]")
            # Блокирующий вызов: главный цикл агента работает,
            # пока его не остановят (CancelledError/KeyboardInterrupt).
            await agent.run()
        except asyncio.CancelledError:
            console.print("\nShutting down...")
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            console.print("\n[red]Gateway crashed[/red]")
            console.print(traceback.format_exc())
        finally:
            # Останавливаем сервисы синхронизации audit_analyzer
            if sync_service is not None:
                try:
                    sync_service.stop(timeout_sec=10.0)
                except Exception:
                    pass
            if store is not None:
                try:
                    # Финальная публикация снимка кеша навыка
                    store.publish()
                except Exception:
                    pass
                try:
                    store.close()
                except Exception:
                    pass
            # Корректная остановка: сначала каналы, затем Streamlit, затем агент.
            channels_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channels_task
            if _streamlit_proc:
                _streamlit_proc.terminate()
                try:
                    _streamlit_proc.wait(timeout=5)
                except Exception:
                    _streamlit_proc.kill()
            if _streamlit_log_handle:
                try:
                    _streamlit_log_handle.close()
                except Exception:
                    pass
            await agent.close_mcp()
            agent.stop()
            await channels.stop_all()
            # Сбрасываем несохранённые сессии на диск
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    # Перезапуск с exponential backoff: 1с → 2с → 4с → 8с → 16с → 30с
    # Если gateway упал с ошибкой, ждём всё дольше, чтобы не спамить
    # БД/Redis переподключениями. После успешного запуска (clean shutdown)
    # задержка сбрасывается, т.к. мы выходим из цикла.
    restart_delay = 1.0
    max_restart_delay = 30.0

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            console.print("\nExiting...")
            break
        except Exception:
            # Падение — ждём и перезапускаемся с увеличивающейся паузой
            console.print(f"[red]Gateway exited unexpectedly, restarting in {restart_delay}s...[/red]")
            console.print(traceback.format_exc())
            time.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, max_restart_delay)
            continue
        break  # clean shutdown — выходим из цикла, процесс завершается


if __name__ == "__main__":
    main()
