"""RuntimePatcher — ВСЕ monkey-patch'и к фреймворку nanobot в одном месте.

Устраняет дублирование между gateway.py и cli_agent.py:

  1. ``patch_context_governor`` — большие результаты инструментов выгружаются
     в ``data_store/`` (ContextGovernor.normalize_tool_result) — было в gateway;
  2. ``patch_assemble_outbound`` — внедрение ``_tool_audit`` в metadata ответа
     (agent._assemble_outbound) — было в gateway И в cli (одинаковый код);
  3. ``patch_subagent_logging`` — БД-логирование подагентов: их tool-события,
     итог запуска (``subagent_run_finished``) и история пишутся в
     ``DbLoggingService`` и ``session_manager`` (SubagentManager использует
     внутренний ``_SubagentHook``, который иначе пишет только debug в loguru).

Каждый патч — в try/except: если API nanobot изменился, патч не применяется,
процесс не падает, причина попадает в ``PatchReport``.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys as _sys
from typing import Any, Optional, Tuple

from loguru import logger

from lib.utils.node_access import get_path as _get


def _getloaded(name: str):
    """Вернуть уже импортированный модуль либо None.

    Используем ``sys.modules`` вместо ``import``: ``import nanobot...``
    резолвит всю цепочку родителей и может падать, если пакет-родитель не
    реэкспортирует вложенный подмодуль. В runtime фреймворк уже импортировал
    целевые модули (shell/exec_session/filesystem/search загружены на старте),
    поэтому они доступны в ``sys.modules``.
    """
    return _sys.modules.get(name)


def _session_key_of(msg: Any) -> str:
    """Вернуть session_key сообщения (``""`` если его нет/не строка).

    Нужен для дренажа аудита конкретной сессии: разные сессии (вопросы)
    обрабатываются конкурентно, и аудит одной сессии не должен попадать
    в ответ другой. ``msg.session_key`` уже равен эффективному ключу —
    ``_dispatch`` нормализует сообщение через ``session_key_override``.
    """
    key = getattr(msg, "session_key", None)
    return key if isinstance(key, str) else ""

def _attach_context_window(agent: Any, session_key: str, result: Any) -> None:
    """Внедрить ``metadata["context_window"]`` в финальный outbound.

    Метрика M1 (занятость окна): ``prompt_tokens`` последней итерации
    оборота (свежий по-итерационный usage из моста ``DatabaseLoggingHook``)
    поделённый на лимит окна модели (``agent.context_window_tokens``).

    Если мост пуст (например, DB-логирование выключено и хука нет), делаем
    фолбэк на накопленный ``agent._last_usage`` — он завышает занятость на
    многоитеративных оборотах (сумма prompt_tokens по всем итерациям),
    поэтому считается запасным вариантом.

    Готовый блок дополнительно кладём в мост: канал читает его в фоновом
    цикле живого обновления и пишет в processing-строку ТОЛЬКО блок (без
    лимита — лимит знает только агент).
    """
    from lib.hooks.database_logging_hook import (
        _store_context_window,
        get_iteration_usage,
    )
    usage = get_iteration_usage(session_key)
    if not usage:
        usage = getattr(agent, "_last_usage", None) or {}
    limit = getattr(agent, "context_window_tokens", None) or 0
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return
    raw_used = (usage or {}).get("prompt_tokens") if isinstance(usage, dict) else None
    try:
        used = int(raw_used or 0)
    except (TypeError, ValueError):
        return
    if used <= 0:
        return
    model = getattr(agent, "model", None)
    block = {
        "used": used,
        "limit": int(limit),
        "pct": round(min(1.0, used / float(limit)), 4),
        "model": model if isinstance(model, str) else "",
    }
    metadata = dict(result.metadata or {})
    metadata["context_window"] = block
    result.metadata = metadata
    _store_context_window(session_key, block)


class PatchReport:
    """Отчёт о применении патчей: что применено / пропущено / упало."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.skipped: list[Tuple[str, str]] = []
        self.failed: list[Tuple[str, str]] = []

    def to_dict(self) -> dict:
        return {
            "applied": list(self.applied),
            "skipped": [list(t) for t in self.skipped],
            "failed": [list(t) for t in self.failed],
        }


class RuntimePatcher:
    """Применение всех локальных доработок к фреймворку nanobot."""

    def apply_all(
        self,
        config: Any,
        settings: Any,
        workspace_dir: Any,
        agent: Any,
        tool_audit_hook: Any,
        *,
        db_logging_service: Any = None,
        session_manager: Any = None,
        recent_files_hook: Any = None,
    ) -> PatchReport:
        """Применить все патчи и вернуть отчёт.

        Args:
            config: runtime-конфиг nanobot (для ``session_key`` в патче).
            settings: ``SETTINGS`` (или его ``.gateway`` секция) — для
                ``persist_threshold``/``persist_max_files``/``persist_max_age_hours``.
            workspace_dir: ``Path`` — корень workspace (для ``data_store/``).
            agent: ``AgentLoop`` (для ``patch_assemble_outbound``).
            tool_audit_hook: ``ToolAuditHook`` (для ``patch_assemble_outbound``).
            recent_files_hook: ``RecentFilesHook`` (опционально, для
                ``patch_assemble_outbound`` — auto-attach созданных файлов
                в ``OutboundMessage.media``).
            db_logging_service: ``DbLoggingService`` (для ``patch_subagent_logging``;
                ``None`` — патч пропускается).
            session_manager: ``SessionManager``/``PGSessionManager`` — для
                персиста истории подагентов (может быть ``None``).

        Returns:
            ``PatchReport`` со списками ``applied`` / ``skipped`` (с причиной).
        """
        report = PatchReport()
        self._record(report, "context_governor", self.patch_context_governor(
            config, settings, workspace_dir))
        self._record(report, "save_turn", self.patch_save_turn(
            settings, workspace_dir, agent))
        self._record(report, "exec_limits", self.patch_exec_limits(settings))
        self._record(report, "tool_limits", self.patch_tool_limits(settings))
        self._record(report, "assemble_outbound", self.patch_assemble_outbound(
            agent, tool_audit_hook, recent_files_hook=recent_files_hook))
        self._record(report, "context_bridge_seed", self.patch_context_bridge_seed(agent))
        self._record(report, "async_save", self.patch_async_session_saves(agent))
        self._record(report, "subagent_logging", self.patch_subagent_logging(
            db_logging_service, session_manager))
        self._record(report, "project_tools", self.patch_project_tools(
            agent, workspace_dir, settings=settings))
        self._record(report, "compact_tracking", self.patch_compaction_tracking(
            agent, settings))
        self._record(report, "compact_command", self.patch_compact_command(
            agent, settings))
        self._record(report, "session_content_cleanup", self.patch_session_content_cleanup())
        return report

    @staticmethod
    def _record(report: PatchReport, name: str, result: Tuple[bool, str]) -> None:
        """Записать результат одного патча в ``PatchReport``.

        True → ``applied``, False → ``skipped`` (с деталью-причиной).
        """
        ok, detail = result
        if ok:
            report.applied.append(name)
        else:
            report.skipped.append((name, detail))

    @staticmethod
    def _format_workspace_hint(workspace_dir: Any) -> str:
        """Краткая подсказка с путём до workspace/tools в лог-сообщении.

        Используется в логах ``patch_project_tools``, чтобы оператор сразу
        видел, откуда грузились tool'ы. Если пути нет — пустая строка.
        """
        if not workspace_dir:
            return ""
        from pathlib import Path as _P

        path = _P(workspace_dir)
        tools_dir = path / "tools"
        if not tools_dir.is_dir():
            return f"(searched: {tools_dir} — not found)"
        count = sum(
            1 for f in tools_dir.glob("*.py") if not f.name.startswith("_")
        )
        plural = "module" if count == 1 else "modules"
        return f"scanned {tools_dir} ({count} {plural})"

    # ------------------------------------------------------------------
    # Патч 2b: seed лимита окна в мост контекста на старте оборота
    # ------------------------------------------------------------------

    def patch_context_bridge_seed(self, agent: Any) -> Tuple[bool, str]:
        """Засеять лимит окна/модель в мост контекста на старте оборота.

        Для ЖИВОГО (по-итерационного) обновления занятости окна канал
        должен знать лимит модели, но знает его только агент. Патч оборачивает
        ``agent._state_build``: каждый оборот (до первых итераций) кладёт
        ``runtime.context_window_tokens`` и модель в мост
        (``lib.hooks.database_logging_hook.seed_context_window``). Хук пишет
        usage каждой итерации, и канал собирает блок на лету.

        Best-effort: при любой ошибке старт оборота продолжается без seed
        (тогда живое обновление недоступно, но финальный снапшот собирает
        ``_attach_context_window`` из фолбэка ``agent._last_usage``).

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        if agent is None:
            return False, "agent is None"
        original = getattr(agent, "_state_build", None)
        if original is None:
            return False, "agent._state_build is missing"
        try:
            from lib.hooks.database_logging_hook import seed_context_window
        except Exception as exc:
            return False, f"import failed: {exc}"

        async def _state_build_with_seed(ctx: Any) -> Any:
            try:
                runtime = ctx.runtime or agent.runtime_for_session(ctx.session)
                limit = getattr(runtime, "context_window_tokens", 0) or 0
                model = getattr(runtime, "model", None) or None
                session_key = getattr(ctx, "session_key", None) or getattr(
                    ctx.session, "key", None
                )
                seed_context_window(session_key, limit=limit, model=model)
            except Exception:
                pass
            return await original(ctx)

        agent._state_build = _state_build_with_seed
        return True, "agent._state_build patched for context-bridge seed"

    # ------------------------------------------------------------------
    # Патч 1: ContextGovernor.normalize_tool_result
    # ------------------------------------------------------------------

    def patch_context_governor(
        self, config: Any, settings: Any, workspace_dir: Any
    ) -> Tuple[bool, str]:
        """Выгружать большие результаты инструментов в data_store/.

        Алгоритм обёртки ``ContextGovernor.normalize_tool_result``:

          1. ``ensure_nonempty_tool_result`` — заменить пустые/None-результаты
             на осмысленные дефолты (нельзя хранить пустоту в контексте LLM);
          2. Если ``tool_name`` в ``_EXEMPT_TOOLS = {"read_file"}`` —
             вернуть как есть (защита от цикла persist → read → persist);
          3. Сериализовать ``result`` в текст (``str`` напрямую, остальное —
             через ``json.dumps``);
          4. Если длина текста > ``persist_threshold`` — сохранить в
             ``data_store/`` (через ``SessionFileStore``) и вернуть
             короткую ссылку ``[Result saved to data_store/<path> (<size> KB)]``;
          5. Иначе — вызвать оригинальный ``normalize_tool_result``.

        Settings читаются из ``settings.gateway.*`` (или эквивалент в
        dict-форме). При ``persist_threshold <= 0`` патч — no-op (это
        штатный способ отключить persist-механизм).

        Returns:
            ``(True, "ContextGovernor.normalize_tool_result patched")``
            при успехе; ``(False, <причина>)`` при отказе (нет атрибута,
            API nanobot изменился и т.п.). При отказе патч НЕ применяется,
            gateway продолжает работу с оригинальным nanobot.
        """
        persist_threshold = int(_get(settings, "gateway", "persist_threshold", default=0) or 0)
        if persist_threshold <= 0:
            return False, "persist_threshold <= 0"

        max_files = int(_get(settings, "gateway", "persist_max_files", default=100) or 100)
        max_age_hours = int(_get(settings, "gateway", "persist_max_age_hours", default=0) or 0)

        try:
            from nanobot.agent.context_governance import ContextGovernor
            from nanobot.utils.runtime import ensure_nonempty_tool_result
            from utils.session_file_store import SessionFileStore, prepare_content
        except Exception as exc:
            return False, f"import failed: {exc}"

        try:
            persisted_store = SessionFileStore(
                workspace_dir / "data_store",
                max_files=max_files,
                max_age_hours=max_age_hours,
            )
            exempt_tools = frozenset({"read_file"})
            original = ContextGovernor.normalize_tool_result

            def _normalize_with_persist(config_, tool_call_id, tool_name, result):
                result = ensure_nonempty_tool_result(tool_name, result)
                if tool_name in exempt_tools:
                    return result

                text = None
                if isinstance(result, str):
                    text = result
                elif not isinstance(result, bytes):
                    try:
                        text = json.dumps(result, ensure_ascii=False, indent=2)
                    except (TypeError, ValueError):
                        pass

                if text is not None and len(text.encode("utf-8")) > persist_threshold:
                    try:
                        content, ext = prepare_content(text)
                        save_info = persisted_store.save(
                            session_key=config_.session_key or "default",
                            content=content,
                            source_tool=tool_name,
                            ext=ext,
                        )
                        return (
                            f"[Result saved to data_store/"
                            f"{save_info['path']} ({save_info['size_kb']} KB)]"
                        )
                    except OSError:
                        pass

                return original(config_, tool_call_id, tool_name, result)

            ContextGovernor.normalize_tool_result = staticmethod(_normalize_with_persist)
            return True, "ContextGovernor.normalize_tool_result patched"
        except Exception as exc:
            return False, f"patch failed: {exc}"

    # ------------------------------------------------------------------
    # Патч 1b: AgentLoop._save_turn → архивация вместо усечения
    # ------------------------------------------------------------------

    def patch_save_turn(
        self, settings: Any, workspace_dir: Any, agent: Any
    ) -> Tuple[bool, str]:
        """Архивировать большие результаты инструментов вместо усечения.

        ``_save_turn`` (nanobot/agent/loop.py) при сохранении истории оборота
        усекает строковые результаты инструментов до ``max_tool_result_chars``
        (по умолчанию 16000 символов), если они не ушли в persist раньше
        (в первую очередь это ``read_file`` и результаты, «проскочившие» мимо
        ``normalize_tool_result``). Это потеря данных: усечённый блоб остаётся
        единственной копией.

        Патч оборачивает ``_save_turn``: любой большой результат
        ``role == "tool"`` (строка или JSON-сериализуемый список) пишется
        **полным** файлом в ``data_store/`` через ``SessionFileStore``, в историю
        кладётся ссылка ``[Result saved to data_store/<path> (<size> KB)]`` —
        в том же формате, что и кастомный persist. Оригинальный ``_save_turn``
        вызывается с копией сообщений (логика nanobot не дублируется).

        Гейт тот же, что у ``patch_context_governor``: при
        ``persist_threshold <= 0`` патч — no-op.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        persist_threshold = int(_get(settings, "gateway", "persist_threshold", default=0) or 0)
        if persist_threshold <= 0:
            return False, "persist_threshold <= 0"
        if agent is None:
            return False, "agent is None"
        original = getattr(agent, "_save_turn", None)
        if original is None:
            return False, "agent._save_turn is missing"

        max_files = int(_get(settings, "gateway", "persist_max_files", default=100) or 100)
        max_age_hours = int(_get(settings, "gateway", "persist_max_age_hours", default=0) or 0)

        try:
            from utils.session_file_store import SessionFileStore, prepare_content
        except Exception as exc:
            return False, f"import failed: {exc}"

        char_limit = int(getattr(agent, "max_tool_result_chars", 16_000) or 16_000)
        try:
            store = SessionFileStore(
                Path(workspace_dir) / "data_store",
                max_files=max_files,
                max_age_hours=max_age_hours,
            )
        except Exception as exc:
            return False, f"store init failed: {exc}"

        def _serialize(content: Any) -> Optional[str]:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                try:
                    return json.dumps(content, ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    return None
            return None

        def _wrap(session, messages, skip, *, turn_latency_ms=None):
            archived = list(messages)
            for idx in range(skip, len(archived)):
                m = archived[idx]
                if not isinstance(m, dict) or m.get("role") != "tool":
                    continue
                text = _serialize(m.get("content"))
                if text is None or len(text.encode("utf-8")) <= char_limit:
                    continue
                try:
                    body, ext = prepare_content(text)
                    session_key = (
                        getattr(session, "key", None)
                        or getattr(session, "session_key", "default")
                        or "default"
                    )
                    info = store.save(
                        session_key=session_key,
                        content=body,
                        source_tool=str(m.get("name") or "tool"),
                        ext=ext,
                        dedupe=True,
                    )
                    m["content"] = (
                        f"[Result saved to data_store/{info['path']} "
                        f"({info['size_kb']} KB)]"
                    )
                except OSError:
                    continue
            return original(session, archived, skip, turn_latency_ms=turn_latency_ms)

        agent._save_turn = _wrap
        return True, "AgentLoop._save_turn patched for archiving"

    # ------------------------------------------------------------------
    # Патч 1b: санитизация контента на источнике (Session.add_message)
    # ------------------------------------------------------------------

    def patch_session_content_cleanup(self) -> Tuple[bool, str]:
        """Вычищать невалидные символы из контента при добавлении сообщения.

        ``nanobot.session.manager.Session.add_message`` — единая точка, через
        которую в сессию попадают все сообщения (user/assistant/tool), в т.ч.
        из web/websocket, подагентов и инструментов. NUL-байт (0x00) и
        литеральные Unicode-escape ``\\u0000``..\\u0003`` могут попасть в
        контент из бинарного вывода инструментов / LLM-вывода и валят запись
        в PostgreSQL (``A string literal cannot contain NUL...``).

        Оборачиваем ``add_message`` и чистим ``content`` и ``**kwargs`` на
        источнике (канонический ``clean_text`` из ``utils.clean_text``), чтобы
        мусор не оседал ни в памяти сессии, ни в JSON-истории, ни в БД.
        Обратный вызов вызывается с очищенными значениями.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        try:
            from nanobot.session.manager import Session
            from utils.clean_text import clean_text
        except Exception as exc:
            return False, f"import failed: {exc}"
        original = getattr(Session, "add_message", None)
        if original is None:
            return False, "Session.add_message is missing"

        def _add_message_clean(                       self: Any,
            role: Any, content: Any, **kwargs: Any,
        ) -> Any:
            return original(self, role, clean_text(content), **clean_text(kwargs))

        Session.add_message = _add_message_clean
        return True, "Session.add_message patched for content cleanup"

    # ------------------------------------------------------------------
    # Патч 1c: синхронный sessions.save из async-контекста → executor
    # ------------------------------------------------------------------

    def patch_async_session_saves(self, agent: Any) -> Tuple[bool, str]:
        """Не блокировать event loop синхронным ``sessions.save()``.

        ``nanobot.agent.loop`` вызывает ``self.sessions.save(...)`` синхронно
        из async-методов (``_state_restore``/``_state_build``/
        ``_state_command``/``_state_save``/``_dispatch``). Пока save ждёт
        в очереди пула БД, event loop заморожен, а async-транзакции канала
        (poll/flush/lease) в это время не могут завершиться — возникает
        взаимная блокировка.

        Патч оборачивает ``agent.sessions.save``:

          * из потока event loop — реальное сохранение выполняется в едином
            последовательном executor (снимок сессии фиксируется на момент
            вызова), вызывающий код возвращается сразу; порядок сохранений
            гарантирован очередью executor'а; ошибки логируются;
          * из остальных потоков (``flush_all``, shutdown, REST-хендлеры) —
            исполняется синхронно, как раньше.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        if agent is None:
            return False, "agent is None"
        sessions = getattr(agent, "sessions", None)
        if sessions is None:
            return False, "agent.sessions is missing"
        original = getattr(sessions, "save", None)
        if original is None:
            return False, "agent.sessions.save is missing"
        try:
            from nanobot.session.manager import Session
        except Exception as exc:
            return False, f"import failed: {exc}"

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
                logger.opt(exception=exc).error(
                    "Async session save failed"
                )

        def _wrapped_save(session: Any, fsync: bool = False) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # вне loop — синхронный вызов, как раньше
                return original(session, fsync=fsync)
            snapshot = _snapshot(session)
            future = executor.submit(original, snapshot, fsync=fsync)
            future.add_done_callback(_log_save_error)
            return None

        sessions.save = _wrapped_save
        sessions._async_save_executor = executor
        return True, "agent.sessions.save wrapped with background executor"

    @staticmethod
    def _bump_schema_max(cls: Any, names: tuple, maximum: int) -> bool:
        """Поднять ``maximum`` у параметров схемы инструмента.

        ``tool_parameters`` хранит схему в замыкании ``parameters``-проперти,
        поэтому мутация исходных ``IntegerSchema`` недоступна. Вместо этого
        оборачиваем ``fget``: после рендера JSON-Schema подменяем ``maximum``
        у нужных параметров. Это влияет и на видимое модели описание, и на
        валидацию (``validate_params`` читает ``parameters``).
        """
        prop = getattr(cls, "parameters", None)
        if not isinstance(prop, property):
            return False
        original = prop.fget
        if original is None:
            return False

        def patched(self):
            d = original(self)
            if isinstance(d, dict):
                props = d.get("properties")
                if isinstance(props, dict):
                    for name in names:
                        frag = props.get(name)
                        if isinstance(frag, dict):
                            frag["maximum"] = maximum
            return d

        cls.parameters = property(patched)
        return True

    # ------------------------------------------------------------------
    # Патч 1c: лимит вывода exec-инструмента (конфигурируемый)
    # ------------------------------------------------------------------

    def patch_exec_limits(self, settings: Any) -> Tuple[bool, str]:
        """Поднять лимит вывода exec/shell-инструмента.

        nanobot режет вывод команды до ``MAX_OUTPUT_CHARS`` (50K символов) и
        вставляет маркер ``... (N chars truncated) ...``, выбрасывая середину
        (``nanobot/agent/tools/shell.py:354-361``, ``exec_session.py:403-413``).
        Output «голова+хвост» потом persist кладёт в файл — данные теряются.

        Патч поднимает потолки вывода из ``settings.gateway.tool_result_limits``
        и делает их конфигурируемыми. В этом проекте это безопасно для контекста:
        вывод exec > ``persist_threshold`` и так уходит полным файлом в
        ``data_store``, а в контекст ставится ссылка (exec не exempt).

        Читаемые ключи:
          * ``exec_max_output_chars`` (дефолт 500_000) — потолок ``MAX_OUTPUT_CHARS``;
          * ``exec_default_output_chars`` (дефолт 100_000) — дефолт ``_MAX_OUTPUT``.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        limits = _get(settings, "gateway", "tool_result_limits", default={}) or {}
        max_out = int(limits.get("exec_max_output_chars", 500_000) or 500_000)
        default_out = int(limits.get("exec_default_output_chars", 100_000) or 100_000)
        if max_out <= 0:
            return False, "exec_max_output_chars <= 0"

        try:
            es = _getloaded("nanobot.agent.tools.exec_session")
            shell = _getloaded("nanobot.agent.tools.shell")
            if es is None or shell is None:
                return False, "exec_session/shell module not loaded"

            # Модульная константа, участвующая в clamp_session_int.
            es.MAX_OUTPUT_CHARS = max_out
            es.DEFAULT_MAX_OUTPUT_CHARS = default_out
            # В shell.py константа импортирована по имени — патчим свою привязку.
            shell.MAX_OUTPUT_CHARS = max_out
            # Дефолт разового exec (когда модель не передаёт max_output_chars).
            shell.ExecTool._MAX_OUTPUT = default_out
            # Схема: чтобы модель могла запросить больше 50K.
            self._bump_schema_max(
                shell.ExecTool, ("max_output_chars", "max_output_tokens"), max_out
            )
            self._bump_schema_max(
                es.WriteStdinTool, ("max_output_chars", "max_output_tokens"), max_out
            )
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, "exec output limits patched"

    # ------------------------------------------------------------------
    # Патч 1d: лимиты read_file / grep / list_dir (конфигурируемые)
    # ------------------------------------------------------------------

    def patch_tool_limits(self, settings: Any) -> Tuple[bool, str]:
        """Поднять потолки инструментов, которые усекают вывод с маркером.

        Читаемые ключи из ``settings.gateway.tool_result_limits``:
          * ``read_file_max_chars`` (дефолт 512_000) — ``ReadFileTool._MAX_CHARS``
            (маркер ``Document text truncated at ~128K chars``);
          * ``grep_head_limit`` (дефолт 500) — ``search._DEFAULT_HEAD_LIMIT``;
          * ``grep_file_head_limit`` (дефолт 400) — ``search._DEFAULT_FILE_HEAD_LIMIT``;
          * ``grep_max_file_bytes`` (дефолт 20_000_000) — ``GrepTool._MAX_FILE_BYTES``
            (файлы больше этого grep пропускает целиком);
          * ``list_dir_max_entries`` (дефолт 500) — ``ListDirTool._DEFAULT_MAX``
            (маркер ``(truncated, showing first N of M entries)``).

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        limits = _get(settings, "gateway", "tool_result_limits", default={}) or {}
        read_max = int(limits.get("read_file_max_chars", 512_000) or 512_000)
        grep_head = int(limits.get("grep_head_limit", 500) or 500)
        grep_file_head = int(limits.get("grep_file_head_limit", 400) or 400)
        grep_max_bytes = int(limits.get("grep_max_file_bytes", 20_000_000) or 20_000_000)
        list_max = int(limits.get("list_dir_max_entries", 500) or 500)
        if read_max <= 0:
            return False, "read_file_max_chars <= 0"

        try:
            fs = _getloaded("nanobot.agent.tools.filesystem")
            srch = _getloaded("nanobot.agent.tools.search")
            if fs is None or srch is None:
                return False, "filesystem/search module not loaded"

            fs.ReadFileTool._MAX_CHARS = read_max
            fs.ListDirTool._DEFAULT_MAX = list_max
            srch._DEFAULT_HEAD_LIMIT = grep_head
            srch._DEFAULT_FILE_HEAD_LIMIT = grep_file_head
            srch.GrepTool._MAX_FILE_BYTES = grep_max_bytes
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, "tool limits patched"

    # ------------------------------------------------------------------
    # Патч 2: agent._assemble_outbound → внедрение _tool_audit
    # ------------------------------------------------------------------

    def patch_assemble_outbound(
        self,
        agent: Any,
        tool_audit_hook: Any,
        recent_files_hook: Any = None,
    ) -> Tuple[bool, str]:
        """Подменить ``agent._assemble_outbound`` обёрткой, дописывающей аудит.

        ``_assemble_outbound`` (см. ``nanobot/agent/loop.py``) формирует
        финальный ``OutboundMessage``. Обёртка вызывает оригинальный метод,
        затем:

          * ``tool_audit_hook.drain(session_key)`` (см.
            ``workspace/hooks/tool_audit_hook.py``) — возвращает и
            обнуляет записи вызовов инструментов, накопленные за оборот
            конкретной сессии. Если они есть — кладём их в
            ``result.metadata["_tool_audit"]``. Каналы и CLI рендерят их в UI.
          * ``recent_files_hook.drain(session_key)`` (если передан; см.
            ``workspace/hooks/recent_files_hook.py``) — возвращает пути
            ко всем файлам, которые агент записал через ``write_file``
            за этот оборот (уже ПОСЛЕ ``SessionFileRedirectHook``, т.е.
            реальные). Подмешиваем их в ``result.media``, сравнивая по
            basename. Закрывает сценарии:

            1. модель забыла приложить созданный файл (``message({...})``
               без ``media``) — добавляем реальный путь;
            2. модель приложила несуществующий путь (``test.docx`` после
               блокировки ``pip install``) — отбрасываем через
               ``Path(p).is_file()``;
            3. модель приложила нереальный абсолютный путь — мы берём
               ``params["path"]`` ПОСЛЕ ``SessionFileRedirectHook``, т.е.
               уже реальный локальный путь;
            4. модель приложила путь, который ЭТИМ же редиректом был
               перенесён в ``data_store/cache/sessions/<key>/`` (basename
               совпадает, но указанный путь не существует на диске) —
               заменяем этот устаревший путь реальным.

        Порядок важен: ``RecentFilesHook`` должен идти **раньше**
        ``ToolAuditHook`` в ``AgentLoop.hooks`` (см. ``ApplicationContext``).

        Args:
            agent: ``AgentLoop``.
            tool_audit_hook: ``ToolAuditHook``.
            recent_files_hook: ``RecentFilesHook`` (опционально; если
                ``None`` — auto-attach отключён).

        Returns:
            ``(True, "agent._assemble_outbound patched")`` при успехе;
            ``(False, <причина>)`` если ``agent is None`` или
            ``_assemble_outbound`` отсутствует (битый nanobot).
        """
        if agent is None:
            return False, "agent is None"
        original = getattr(agent, "_assemble_outbound", None)
        if original is None:
            return False, "agent._assemble_outbound is missing"

        def _wrap(msg, final_content, all_msgs, stop_reason, had_injections,
                  on_stream, *, turn_latency_ms=None):
            from lib.utils.outbound_meta import FINAL_TURN_KEY as _FINAL_TURN
            result = original(
                msg, final_content, all_msgs, stop_reason, had_injections,
                on_stream, turn_latency_ms=turn_latency_ms,
            )
            if result is None:
                # ``_assemble_outbound`` возвращает None только при подавлении
                # финала из-за ``MessageTool`` (``_sent_in_turn`` +
                # «пустой финал»). Тогда канал НЕ получит финального outbound
                # и не сможет корректно финализировать слот/клейм — оборот
                # зависнет и упрётся в reclaim → failed. Публикуем
                # синтетический маркер конца оборота, чтобы канал закрыл
                # оборот (см. ``PostgresChannel.send``).
                if msg is None:
                    return None  # unittest-путь; строить синтетику не из чего
                try:
                    from nanobot.bus.events import OutboundMessage
                except Exception:
                    return None
                result = OutboundMessage(
                    channel=getattr(msg, "channel", None),
                    chat_id=getattr(msg, "chat_id", None),
                    content="",
                    metadata={
                        **(getattr(msg, "metadata", None) or {}),
                        _FINAL_TURN: True,
                    },
                )
            else:
                # Маркер конца оборота: канал отличает финальный outbound
                # от промежуточных публикаций ``message(...)``.
                metadata = dict(result.metadata or {})
                metadata[_FINAL_TURN] = True
                result.metadata = metadata
            session_key = _session_key_of(msg)

            # 1) Tool audit → result.metadata["_tool_audit"]
            if tool_audit_hook is not None:
                entries = tool_audit_hook.drain(session_key)
                if entries:
                    result.metadata["_tool_audit"] = entries

            # 2) Auto-attach recent files → result.media
            if recent_files_hook is not None:
                recent = recent_files_hook.drain(session_key)
                if recent:
                    media = list(result.media or [])
                    # basename -> индексы уже указанных media-путей.
                    by_name: dict = {}
                    for i, m in enumerate(media):
                        if isinstance(m, str) and m:
                            by_name.setdefault(Path(m).name, []).append(i)

                    seen: set = set()
                    for p in recent:
                        p_path = Path(p)
                        if not p_path.is_file():
                            continue
                        name = p_path.name
                        if name in seen:
                            continue
                        idxs = by_name.get(name)
                        if idxs is None:
                            # Нет записи с таким именем — просто добавляем
                            # реальный путь.
                            media.append(str(p_path))
                            seen.add(name)
                            continue
                        # Запись с этим basename уже есть в media.
                        if any(
                            isinstance(media[i], str) and Path(media[i]).is_file()
                            for i in idxs
                        ):
                            # Среди указанных путей уже есть живой файл с этим
                            # именем — не дублируем.
                            seen.add(name)
                            continue
                        # Модель приложила путь ДО SessionFileRedirectHook, т.е.
                        # реальный файл лежит по перенаправленному пути, а в
                        # media — устаревший (несуществующий). Заменяем первый
                        # такой путь реальным.
                        for i in idxs:
                            if isinstance(media[i], str) and not Path(media[i]).is_file():
                                media[i] = str(p_path)
                                break
                        seen.add(name)

                    result.media = media

            # 3) Контекстное окно → result.metadata["context_window"]
            _attach_context_window(agent, session_key, result)

            return result

        agent._assemble_outbound = _wrap
        return True, "agent._assemble_outbound patched"

    # ------------------------------------------------------------------
    # Патч 3: SubagentManager._SubagentHook → БД-логирование подагентов
    # ------------------------------------------------------------------

    def patch_subagent_logging(
        self, db_logging_service: Any, session_manager: Any = None
    ) -> Tuple[bool, str]:
        """Логировать подагентов: tool-события, итог запуска и историю.

        ``SubagentManager._run_subagent`` (``nanobot/agent/subagent.py``)
        исполняет подагента через ``AgentRunner.run(AgentRunSpec(hook=
        _SubagentHook(task_id, status)))`` — внутренний ``_SubagentHook``
        пишет только статус и debug в loguru, в БД ничего не попадает.

        Патч заменяет класс ``nanobot.agent.subagent._SubagentHook`` на
        подкласс, который дополнительно:

          1. проксирует tool-события подагента (call/result/error) в
             ``DatabaseLoggingHook`` → ``DbLoggingService``;
          2. пишет итог запуска как ``subagent_run_finished``;
          3. персистит историю подагента (``context.messages``) в
             ``session_manager`` под ключом ``subagent:<task_id>``.

        События подагента получают ``session_id`` вида
        ``<origin>:subagent:<task_id>`` (или ``subagent:<task_id>`` без
        origin) — их легко отличить от событий основного агента и связать
        с конкретным запуском. ``channel`` для итога — ``subagent``.

        История пишется один раз на запуск: guard-флаг ``_finalized``
        исключает дубликат, когда у runner вызываются и ``on_error``, и
        ``after_run`` (путь tool_error), а при hard-exception — только
        ``on_error``.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` если
            ``db_logging_service`` не передан или API nanobot изменился
            (патч пропускается, подагенты продолжают работать как раньше).
        """
        if db_logging_service is None:
            return False, "db_logging_service is None"
        try:
            from nanobot.agent.subagent import _SubagentHook
            from lib.hooks.database_logging_hook import DatabaseLoggingHook
            from lib.services.db_logging_service import LogEvent
        except Exception as exc:
            return False, f"import failed: {exc}"

        class _SubagentLoggingHook(_SubagentHook):
            """_SubagentHook + БД-логирование + персист истории подагента."""

            _sessions = session_manager

            def __init__(self, task_id, status=None):
                super().__init__(task_id, status)
                self._task_id = str(task_id)
                self._session_id = f"subagent:{self._task_id}"
                self._finalized = False
                # Свой инстанс DatabaseLoggingHook на ЗАПУСК подагента.
                # Не разделяется ни между субагентами, ни с основным
                # оборотом — иначе конкурентные субагенты перезаписывали
                # бы _request_id/_run_session_key друг друга.
                self._db_hook = DatabaseLoggingHook(db_logging_service)
                self._parent_rid = None

            def _subagent_session_key(self, context) -> str:
                """``<origin>:subagent:<task_id>`` или ``subagent:<task_id>``."""
                origin = getattr(context, "session_key", None) or ""
                return f"{origin}:{self._session_id}" if origin else self._session_id

            def _ensure_request(self, context) -> None:
                """Зарегистрировать контекст подагента в agent_question_runs (upsert)."""
                if self._parent_rid is None:
                    origin = getattr(context, "session_key", None) or ""
                    self._parent_rid = (
                        self._db_hook._service.get_request_id(origin)
                        or self._task_id
                    )
                key = self._subagent_session_key(context)
                self._db_hook._service.register_request(
                    key,
                    self._session_id,   # request_id подагента = subagent:<task_id>
                    parent_request_id=self._parent_rid,
                    agent_id=self._session_id,
                    parent_agent_id=self._db_hook._agent_id,
                    is_subagent=True,
                    status="running",
                )

            async def before_execute_tool(self, context, tool_call, tool, params):
                self._ensure_request(context)
                key = self._subagent_session_key(context)
                orig = context.session_key
                ctx_session = self._db_hook._run_session_key
                ctx_rid = self._db_hook._request_id
                context.session_key = key
                try:
                    await self._db_hook.before_execute_tool(
                        context, tool_call, tool, params
                    )
                finally:
                    context.session_key = orig
                    # вложенный вызов не должен портить состояние
                    # основного прогона (его after_run читает эти поля)
                    self._db_hook._run_session_key = ctx_session
                    self._db_hook._request_id = ctx_rid

            async def after_execute_tool(
                self, context, tool_call, tool, params, result
            ):
                self._ensure_request(context)
                key = self._subagent_session_key(context)
                orig = context.session_key
                ctx_session = self._db_hook._run_session_key
                ctx_rid = self._db_hook._request_id
                context.session_key = key
                try:
                    await self._db_hook.after_execute_tool(
                        context, tool_call, tool, params, result
                    )
                finally:
                    context.session_key = orig
                    self._db_hook._run_session_key = ctx_session
                    self._db_hook._request_id = ctx_rid

            async def on_execute_tool_error(
                self, context, tool_call, tool, params, error
            ):
                self._ensure_request(context)
                key = self._subagent_session_key(context)
                orig = context.session_key
                ctx_session = self._db_hook._run_session_key
                ctx_rid = self._db_hook._request_id
                context.session_key = key
                try:
                    await self._db_hook.on_execute_tool_error(
                        context, tool_call, tool, params, error
                    )
                finally:
                    context.session_key = orig
                    self._db_hook._run_session_key = ctx_session
                    self._db_hook._request_id = ctx_rid

            async def after_run(self, context):
                await self._finalize(context)

            async def on_error(self, context):
                # runner вызывает on_error до after_run в путях с error —
                # guard-флаг исключает двойную запись истории/итога
                await self._finalize(context)

            async def _finalize(self, context):
                if self._finalized:
                    return
                self._finalized = True
                self._ensure_request(context)
                key = self._subagent_session_key(context)
                try:
                    self._persist_history(context)
                except Exception:
                    pass
                try:
                    final = context.final_content or ""
                    task = self._extract_task(context)
                    self._db_hook._service.log_event(LogEvent(
                        event_type="subagent_run_finished",
                        level="ERROR" if context.error else "INFO",
                        session_id=self._session_id,
                        channel="subagent",
                        actor="agent",
                        name=self._task_id,
                        request_id=self._session_id,
                        summary=(task or final)[:200],
                        payload={
                            "final_content": final,
                            "tools_used": list(context.tools_used or []),
                            "stop_reason": context.stop_reason,
                            "task_id": self._task_id,
                            "task": task,
                            "request_id": self._session_id,
                            "parent_request_id": self._parent_rid,
                        },
                        metadata={
                            "tokens_used": (context.usage or {}).get("total_tokens"),
                            "had_error": bool(context.error),
                        },
                    ))
                    self._db_hook._service.finish_request(
                        self._session_id,
                        status="error" if context.error else "finished",
                        summary=(task or final)[:200] or None,
                        response=final or None,
                    )
                except Exception:
                    pass
                finally:
                    self._db_hook._service.clear_request(key)

            @staticmethod
            def _extract_task(context) -> Optional[str]:
                """Извлечь описание задачи подагента (первое user-сообщение)."""
                msgs = list(getattr(context, "messages", None) or [])
                for m in msgs:
                    if m.get("role") == "user":
                        content = m.get("content")
                        if isinstance(content, str):
                            return content[:500]
                        if isinstance(content, list):
                            parts = []
                            for blk in content:
                                if isinstance(blk, dict) and blk.get("type") == "text":
                                    parts.append(blk.get("text", ""))
                            return "".join(parts)[:500]
                return None

            def _persist_history(self, context):
                msgs = list(getattr(context, "messages", None) or [])
                if not msgs or self._sessions is None:
                    return
                session = self._sessions.get_or_create(self._session_id)
                for m in msgs:
                    role = m.get("role")
                    if role == "system":
                        continue
                    content = m.get("content")
                    if not isinstance(content, str):
                        try:
                            content = json.dumps(content, ensure_ascii=False)
                        except (TypeError, ValueError):
                            content = str(content) if content is not None else ""
                    kwargs = {}
                    if m.get("tool_calls"):
                        kwargs["tool_calls"] = m["tool_calls"]
                    if m.get("tool_call_id"):
                        kwargs["tool_call_id"] = m["tool_call_id"]
                    if m.get("name"):
                        kwargs["name"] = m["name"]
                    if m.get("reasoning_content"):
                        kwargs["reasoning_content"] = m["reasoning_content"]
                    if m.get("thinking_blocks"):
                        kwargs["thinking_blocks"] = m["thinking_blocks"]
                    session.add_message(role, content, **kwargs)
                self._sessions.save(session)

        try:
            import nanobot.agent.subagent as _subagent_mod
            _subagent_mod._SubagentHook = _SubagentLoggingHook
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, "SubagentManager._SubagentHook patched for DB logging"

    # ------------------------------------------------------------------
    # Патч 4: auto-discover и регистрация пользовательских tool'ов
    #         из workspace/tools/*.py
    # ------------------------------------------------------------------

    def patch_project_tools(
        self, agent: Any, workspace_dir: Any,
        *, settings: Any = None,
    ) -> Tuple[bool, str]:
        """Зарегистрировать кастомные tool'ы из ``workspace/tools/*.py``.

        Использует встроенные механизмы nanobot:

          * ``pkgutil.iter_modules`` по ``workspace/tools/`` (как
            ``ToolLoader.discover`` в ``nanobot/agent/tools/loader.py:37``);
          * ``Tool.enabled(ctx)`` / ``Tool.create(ctx)`` (как
            ``ToolLoader.load`` в ``loader.py:86-118``);
          * ``ToolRegistry.register`` (см.
            ``nanobot/agent/tools/registry.py:30``).

        ``ToolContext`` собирается из полей ``AgentLoop`` тем же способом,
        что в ``AgentLoop._register_default_tools`` (``loop.py:597-630``).
        В вашей версии nanobot ``ToolContext.__init__`` не принимает
        ``metadata``, поэтому дополнительные DI-ссылки (``agent``,
        ``settings``) пробрасываются через ``setattr``:

          * ``ctx._agent_ref`` — ``AgentLoop`` (для tool'ов, которым нужен
            ``agent.consolidator`` и т.п.);
          * ``ctx._settings_ref`` — ``SETTINGS`` (для чтения ``gateway.*``
            секций, не дублированных в ``config.tools.*``).

        Конфликты имён (например, если свой tool назван ``exec``) не
        затирают встроенные — те, что уже в ``agent.tools``, пропускаются.

        Args:
            agent: ``AgentLoop``.
            workspace_dir: ``Path`` — корень workspace, в нём лежит
                ``tools/`` с модулями кастомных tool'ов.
            settings: ``SETTINGS`` (опционально) — для ``ctx._settings_ref``.
                Если ``None``, tool'ы, которым нужен settings, получат
                ``None`` и сами решают, как с этим жить.

        Returns:
            ``(True, "<N> tools registered: <names>")`` или
            ``(False, "<причина>")``.
        """
        if agent is None:
            return False, "agent is None"
        try:
            import importlib.util
            import pkgutil
            import sys as _sys

            from pathlib import Path as _P

            tools_dir = _P(workspace_dir) / "tools"
            if not tools_dir.is_dir():
                return True, "workspace/tools not found — skip"

            imported: list[str] = []
            for _imp, mod_name, _ispkg in pkgutil.iter_modules([str(tools_dir)]):
                if mod_name.startswith("_"):
                    continue
                full = f"workspace.tools.{mod_name}"
                if full in _sys.modules:
                    continue
                try:
                    file_path = tools_dir / f"{mod_name}.py"
                    spec = importlib.util.spec_from_file_location(
                        full, str(file_path)
                    )
                    if spec is None or spec.loader is None:
                        logger.warning(
                            "Failed to build spec for {}", full
                        )
                        continue
                    module = importlib.util.module_from_spec(spec)
                    _sys.modules[full] = module
                    try:
                        spec.loader.exec_module(module)
                        imported.append(full)
                    except Exception:
                        _sys.modules.pop(full, None)
                        raise
                except Exception:
                    logger.exception("Failed to import {}", full)

            from nanobot.agent.tools.base import Tool as _T

            candidates: list[type] = []
            seen_ids: set[int] = set()
            for mod_name in list(_sys.modules):
                if not mod_name.startswith("workspace.tools."):
                    continue
                module = _sys.modules.get(mod_name)
                if module is None:
                    continue
                for attr_name in dir(module):
                    cls = getattr(module, attr_name, None)
                    if not (isinstance(cls, type) and issubclass(cls, _T)):
                        continue
                    if cls is _T:
                        continue
                    if getattr(cls, "__abstractmethods__", None):
                        continue
                    if id(cls) in seen_ids:
                        continue
                    seen_ids.add(id(cls))
                    candidates.append(cls)

            if not candidates:
                return True, (
                    "no project tools found" + (
                        f" (imported: {', '.join(imported)})" if imported else ""
                    )
                )

            from nanobot.agent.tools.context import ToolContext

            ctx = ToolContext(
                config=getattr(agent, "tools_config", None),
                workspace=str(getattr(agent, "workspace", workspace_dir)),
                bus=getattr(agent, "bus", None),
                subagent_manager=getattr(agent, "subagents", None),
                cron_service=getattr(agent, "cron_service", None),
                exec_session_manager=getattr(agent, "_exec_session_manager", None),
                sessions=getattr(agent, "sessions", None),
                file_state_store=getattr(agent, "file_states", None),
                provider_snapshot_loader=getattr(
                    agent, "provider_snapshot_loader", None
                ),
                image_generation_provider_configs=getattr(
                    agent, "_image_generation_provider_configs", None
                ),
                timezone=getattr(
                    getattr(agent, "context", None), "timezone", "UTC"
                ) or "UTC",
                workspace_sandbox=getattr(
                    getattr(agent, "workspace_scopes", None),
                    "sandbox_status", None
                ),
                runtime_events=getattr(agent, "runtime_events", None),
            )
            # ``agent`` не входит в ToolContext по контракту nanobot — кладём
            # отдельным атрибутом, чтобы tool'ы с DI-сервисами (например,
            # CompactContextTool) могли его получить через
            # ``getattr(ctx, "_agent_ref", None)``.
            setattr(ctx, "_agent_ref", agent)
            if settings is not None:
                setattr(ctx, "_settings_ref", settings)

            registered: list[str] = []
            skipped_disabled: list[str] = []
            skipped_duplicate: list[str] = []
            failed: list[str] = []
            for cls in candidates:
                try:
                    if not cls.enabled(ctx):
                        skipped_disabled.append(cls.__name__)
                        continue
                    tool = cls.create(ctx)
                    if agent.tools.get(tool.name) is not None:
                        skipped_duplicate.append(tool.name)
                        continue
                    agent.tools.register(tool)
                    registered.append(tool.name)
                except Exception:
                    logger.exception("Failed to register {}", cls.__name__)
                    failed.append(cls.__name__)

            detail = f"{len(registered)} project tools registered"
            if registered:
                detail += f": {', '.join(registered)}"
            if skipped_disabled:
                detail += (
                    f"; {len(skipped_disabled)} disabled by config: "
                    f"{', '.join(skipped_disabled)}"
                )
            if skipped_duplicate:
                detail += (
                    f"; {len(skipped_duplicate)} already registered: "
                    f"{', '.join(skipped_duplicate)}"
                )
            if failed:
                detail += f"; {len(failed)} failed: {', '.join(failed)}"
            # Логируем итог через INFO — иначе пользователь не видит,
            # что проектные tool'ы реально подхватились (в nanobot
            # ``Registered N tools`` логируется только для builtin
            # внутри ``AgentLoop._register_default_tools``).
            logger.info(
                "Custom (project) tools: {} — {}",
                detail,
                self._format_workspace_hint(workspace_dir),
            )
            return True, detail

        except Exception as exc:
            logger.exception("patch_project_tools failed: {}", exc)
            return False, f"patch failed: {exc}"

    # ------------------------------------------------------------------
    # Патч 5: авто-сжатие → заметка в agent_conversation_messages
    # ------------------------------------------------------------------

    def patch_compaction_tracking(
        self, agent: Any, settings: Any
    ) -> Tuple[bool, str]:
        """Обернуть авто-сжатие так, чтобы оно шло через тот же путь,
        что и ручной ``/compact``: тот же отчёт, та же запись в историю.

        Оборачивает две штатные точки nanobot:

          * ``agent.auto_compact._archive`` — idle-сжатие простаивающих
            сессий (``AutoCompact.check_expired`` → ``_archive``);
          * ``agent.consolidator.maybe_consolidate_by_tokens`` —
            token-budget/replay-window сжатие на каждом ``_state_build``
            /``_state_save``.

        После оригинального метода обёртка сравнивает состояние сессии
        (``last_consolidated`` до/после) и при факте архивации зовёт
        ``ContextCompactionService.record_external_compaction(...)``,
        который собирает отчёт и пишет заметку в ``agent_conversation_messages``
        ровно тем же кодом, что и ручной ``compact()`` (та же функция
        ``_write_history_notice``, тот же ``format_report``).

        При ``gateway.compact.enabled=false`` или
        ``gateway.compact.notify_in_history=false`` патч — no-op.
        """
        if agent is None:
            return False, "agent is None"
        try:
            from lib.services.context_compaction import ContextCompactionService
        except Exception as exc:
            return False, f"import failed: {exc}"
        try:
            svc = ContextCompactionService(agent, settings=settings)
            if not svc.enabled:
                return False, "gateway.compact.enabled=false"
            if not svc.notify_in_history:
                return False, "gateway.compact.notify_in_history=false"
            self._wrap_auto_compact_archive(agent, svc)
            self._wrap_maybe_consolidate_by_tokens(agent, svc)
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, "auto compaction tracking patched"

    def patch_compact_command(
        self, agent: Any, settings: Any
    ) -> Tuple[bool, str]:
        """Зарегистрировать команду ``/compact`` в ``CommandRouter`` агента.

        ``/compact`` — это настоящая slash-команда (по образцу ``cmd_new``
        из ``nanobot/command/builtin.py``). На любом канале (postgres,
        streamlit, telegram) она срабатывает ДЕТЕРМИНИРОВАННО ДО LLM:
        ``run()`` видит зарегистрированную команду в router'е и
        обрабатывает её через ``_state_command`` / ``_dispatch_command_inline``,
        не отправляя сообщение модели. Так ``/compact`` всегда сжимает
        сессию безоговорочно (``force=True``), а не «по усмотрению» LLM.

        Без этой регистрации ``/compact`` уходит в LLM как обычное
        user-сообщение, и модель часто отвечает текстом «сжатие не
        требуется», не вызывая tool — это и есть исходная проблема.

        Регистрируем:
          * ``exact("/compact")`` — точное совпадение;
          * ``prefix("/compact ")`` — ``/compact idle`` (для совместимости).
        """
        from functools import partial

        from lib.commands.compact_command import cmd_compact

        commands = getattr(agent, "commands", None)
        if commands is None:
            return False, "agent.commands is missing"
        handler = partial(cmd_compact, settings=settings)
        try:
            commands.exact("/compact", handler)
            commands.prefix("/compact ", handler)
        except Exception as exc:
            return False, f"register failed: {exc}"
        return True, "/compact registered as slash command"

    @staticmethod
    def _wrap_auto_compact_archive(agent: Any, svc: Any) -> None:
        """Обернуть ``AutoCompact._archive`` (idle auto-compact)."""
        auto = getattr(agent, "auto_compact", None)
        if auto is None:
            return
        original = getattr(auto, "_archive", None)
        if original is None:
            return

        async def _wrapped(key: str, *, runtime: Any) -> Any:
            sessions = agent.sessions
            before_session = sessions.get_or_create(key)
            before_cursor = int(getattr(before_session, "last_consolidated", 0) or 0)
            before_tokens, _ = await svc._estimate(before_session, runtime)
            result = await original(key, runtime=runtime)
            fresh = sessions.get_or_create(key)
            after_cursor = int(getattr(fresh, "last_consolidated", 0) or 0)
            if after_cursor > before_cursor and result not in (None, "", "(nothing)"):
                after_tokens, _ = await svc._estimate(fresh, runtime)
                await svc.record_external_compaction(
                    session_key=key, mode="idle",
                    summary=result,
                    archived_msgs=after_cursor - before_cursor,
                    kept_msgs=len(getattr(fresh, "messages", []) or []),
                    tokens_before=before_tokens,
                    tokens_after=after_tokens,
                )
            return result

        auto._archive = _wrapped

    @staticmethod
    def _wrap_maybe_consolidate_by_tokens(agent: Any, svc: Any) -> None:
        """Обернуть ``Consolidator.maybe_consolidate_by_tokens`` (token auto-compact)."""
        consolidator = getattr(agent, "consolidator", None)
        if consolidator is None:
            return
        original = getattr(consolidator, "maybe_consolidate_by_tokens", None)
        if original is None:
            return

        async def _wrapped(session: Any, **kwargs: Any) -> Any:
            sessions = agent.sessions
            key = getattr(session, "key", None)
            before_cursor = int(getattr(session, "last_consolidated", 0) or 0)
            runtime = kwargs.get("runtime")
            before_tokens, _ = (
                await svc._estimate(session, runtime) if runtime else (0, "")
            )
            await original(session, **kwargs)
            if not key:
                return
            fresh = sessions.get_or_create(key)
            after_cursor = int(getattr(fresh, "last_consolidated", 0) or 0)
            if after_cursor > before_cursor:
                after_meta = (getattr(fresh, "metadata", {}) or {})
                summary_obj = after_meta.get("_last_summary")
                summary = (
                    summary_obj.get("text") if isinstance(summary_obj, dict)
                    else summary_obj if isinstance(summary_obj, str) else None
                )
                after_tokens, _ = (
                    await svc._estimate(fresh, runtime) if runtime else (0, "")
                )
                await svc.record_external_compaction(
                    session_key=key, mode="token",
                    summary=summary,
                    archived_msgs=after_cursor - before_cursor,
                    kept_msgs=len(getattr(fresh, "messages", []) or []),
                    tokens_before=before_tokens,
                    tokens_after=after_tokens,
                )

        consolidator.maybe_consolidate_by_tokens = _wrapped
