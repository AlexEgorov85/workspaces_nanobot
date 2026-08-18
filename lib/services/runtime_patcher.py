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

import json
import os
from typing import Any, Optional, Tuple

from lib.utils.node_access import get_path as _get


def _session_key_of(msg: Any) -> str:
    """Вернуть session_key сообщения (``""`` если его нет/не строка).

    Нужен для дренажа аудита конкретной сессии: разные сессии (вопросы)
    обрабатываются конкурентно, и аудит одной сессии не должен попадать
    в ответ другой. ``msg.session_key`` уже равен эффективному ключу —
    ``_dispatch`` нормализует сообщение через ``session_key_override``.
    """
    key = getattr(msg, "session_key", None)
    return key if isinstance(key, str) else ""


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
    ) -> PatchReport:
        """Применить все патчи и вернуть отчёт.

        Args:
            config: runtime-конфиг nanobot (для ``session_key`` в патче).
            settings: ``SETTINGS`` (или его ``.gateway`` секция) — для
                ``persist_threshold``/``persist_max_files``/``persist_max_age_hours``.
            workspace_dir: ``Path`` — корень workspace (для ``data_store/``).
            agent: ``AgentLoop`` (для ``patch_assemble_outbound``).
            tool_audit_hook: ``ToolAuditHook`` (для ``patch_assemble_outbound``).
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
        self._record(report, "assemble_outbound", self.patch_assemble_outbound(
            agent, tool_audit_hook))
        self._record(report, "subagent_logging", self.patch_subagent_logging(
            db_logging_service, session_manager))
        # Патч 4: MessageTool.execute — лечение, не костыль: подмешиваем
        # в media свежие файлы, если бот вызвал message без media.
        self._record(report, "message_tool", self.patch_message_tool(agent))
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
    # Патч 2: agent._assemble_outbound → внедрение _tool_audit
    # ------------------------------------------------------------------

    def patch_assemble_outbound(
        self, agent: Any, tool_audit_hook: Any
    ) -> Tuple[bool, str]:
        """Подменить ``agent._assemble_outbound`` обёрткой, дописывающей аудит.

        ``_assemble_outbound`` (см. ``nanobot/agent/loop.py``) формирует
        финальный ``OutboundMessage``. Обёртка вызывает оригинальный метод,
        затем ``tool_audit_hook.drain(session_key)`` (см.
        ``workspace/hooks/tool_audit_hook.py``) — он возвращает и
        обнуляет записи вызовов инструментов, накопленные за оборот
        конкретной сессии. Сессия определяется из ``msg``: разные сессии
        (вопросы) обрабатываются конкурентно, поэтому дренируется только
        аудит текущего вопроса.
        Если записи есть — кладём их в ``result.metadata["_tool_audit"]``.

        Каналы и CLI читают этот ключ и рендерят записи в UI
        (``✓ read(x.txt) → content`` / ``✗ exec: timeout``).

        Дополнительно: обёртка дренирует ``AutoAttachRegistry`` (см.
        ``workspace/hooks/auto_attach_hook.py``) и прикрепляет свежие
        файлы к ``result.media`` — на случай если бот забыл вызвать
        ``message(content, media=[path])``. Это страховка от рассинхрона
        LLM-инструкций и поведения: в большинстве случаев бот всё же
        прикрепляет файлы сам (``message`` tool кладёт ``media`` в
        ``OutboundMessage.media`` через ``publisher``), тогда мы не
        дописываем дубликаты — список дедуплицируется.

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

        # Ленивый импорт ``AutoAttachRegistry`` — модуль может быть
        # недоступен в тестах без полного workspace, и тогда auto-attach
        # просто не работает (это безопасно: старый путь через
        # ``message(content, media=[path])`` остаётся).
        try:
            from workspace.hooks.auto_attach_hook import AutoAttachRegistry
        except Exception:
            AutoAttachRegistry = None  # type: ignore[assignment]

        def _wrap(msg, final_content, all_msgs, stop_reason, had_injections,
                  on_stream, *, turn_latency_ms=None):
            result = original(
                msg, final_content, all_msgs, stop_reason, had_injections,
                on_stream, turn_latency_ms=turn_latency_ms,
            )
            if result is None:
                return result
            sk = _session_key_of(msg)
            entries = tool_audit_hook.drain(sk)
            if entries:
                result.metadata["_tool_audit"] = entries
            # Auto-attach: добавляем свежие файлы, если бот не прикрепил
            # их сам через ``message``. Дубликаты убираем.
            if AutoAttachRegistry is not None:
                fresh = AutoAttachRegistry.drain(sk)
                if fresh:
                    existing = list(result.media or [])
                    seen = {os.path.normpath(p) for p in existing}
                    for path in fresh:
                        np = os.path.normpath(path)
                        if np in seen:
                            continue
                        existing.append(path)
                        seen.add(np)
                    if existing:
                        result.media = existing
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
            from workspace.hooks.database_logging_hook import DatabaseLoggingHook
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
    # Патч 4: MessageTool.execute — автоприкрепление файлов
    # ------------------------------------------------------------------
    #
    # ЛЕЧЕНИЕ, а не костыль: tool ``message`` в nanobot 0.3.0 имеет системный
    # промпт, который разрешает вызывать его в текущем чате **только** по
    # явной просьбе пользователя. LLM читает «Do not use this for the normal
    # reply in the current chat» и интерпретирует «создай файл и прикрепи
    # в чат» как «normal reply» → message не вызывается → файл остаётся
    # только в ``data_store/cache/``, в ``agent_conversation_messages.media``
    # пусто, Streamlit вложение не показывает.
    #
    # Чтобы вылечить это без правки upstream (nanobot) и без нового tool,
    # мы wrap'аем ``MessageTool.execute``: если бот вызвал ``message``
    # БЕЗ media (что и есть «забыл»), мы подмешиваем в media свежие
    # файлы из ``AutoAttachRegistry`` (per-turn bucket). Это эквивалентно
    # тому, как если бы LLM сам вызвал ``message(content, media=[path])``,
    # но без требования помнить про параметр.
    #
    # Дедупликация: если бот всё-таки передал часть файлов, мы добавляем
    # только те свежие, которых нет в его media. Никаких дублей.
    #
    # Patch применяется в ``apply_all`` после остальных; если tool
    # ``message`` отсутствует (CLI-режим без MessageTool) — патч
    # пропускается без ошибки.

    def patch_message_tool(self, agent: Any) -> Tuple[bool, str]:
        """Wrap ``MessageTool.execute`` — подмешивает свежие файлы в media.

        Условия:
          * tool ``message`` зарегистрирован в ``agent.tools`` и это
            ``nanobot.agent.tools.message.MessageTool``;
          * ``MessageTool.execute`` ещё не wrapped (idempotent patch).

        Returns:
            ``(True, "MessageTool.execute wrapped")`` при успехе;
            ``(False, <причина>)`` если tool отсутствует или API
            nanobot изменился.
        """
        if agent is None:
            return False, "agent is None"
        try:
            from nanobot.agent.tools.message import MessageTool
        except Exception as exc:
            return False, f"import failed: {exc}"

        message_tool = agent.tools.get("message")
        if not isinstance(message_tool, MessageTool):
            return False, "message tool is missing or not MessageTool"

        # Idempotent: если уже wrapped — повторно не накатываем.
        if getattr(message_tool.execute, "_audit_track_attached", False):
            return True, "MessageTool.execute already wrapped"

        # Ленивый импорт AutoAttachRegistry — без него патч no-op,
        # MessageTool работает ровно как раньше.
        try:
            from workspace.hooks.auto_attach_hook import AutoAttachRegistry
        except Exception:
            AutoAttachRegistry = None  # type: ignore[assignment]

        original_execute = message_tool.execute

        async def _wrapped_execute(
            content: str,
            channel: Optional[str] = None,
            chat_id: Optional[str] = None,
            message_id: Optional[str] = None,
            media: Optional[list] = None,
            buttons: Any = None,
            **kwargs: Any,
        ):
            """Wrap execute: подмешать свежие файлы в media, если бот забыл.

            Логика:
              1. ``media`` пустой (None или [])?
              2. Узнаём ``session_key`` текущего оборота через
                 ``MessageTool._fallback_*`` (выставляется gateway'ом)
                 или через ``current_request_context()``.
              3. Дренируем ``AutoAttachRegistry.drain(session_key)``.
              4. Если есть свежие файлы — добавляем в ``media`` (дедуп
                 по ``os.path.normpath``).
              5. Делегируем ``original_execute`` с дополненным media.
            """
            if AutoAttachRegistry is not None:
                # Шаг 1-2: session_key.
                session_key: Optional[str] = None
                try:
                    from nanobot.agent.tools.context import current_request_context
                    ctx = current_request_context()
                    if ctx is not None:
                        session_key = getattr(ctx, "session_key", None)
                except Exception:
                    pass
                if not session_key and (
                    message_tool._fallback_channel
                    and message_tool._fallback_chat_id
                ):
                    session_key = (
                        f"{message_tool._fallback_channel}:"
                        f"{message_tool._fallback_chat_id}"
                    )

                # Шаг 3: дренировать свежие файлы.
                if session_key:
                    fresh = AutoAttachRegistry.drain(session_key)
                    if fresh:
                        # Шаг 4: дедуп по нормализованному пути.
                        existing = list(media or [])
                        seen = {os.path.normpath(p) for p in existing}
                        for path in fresh:
                            np = os.path.normpath(path)
                            if np in seen:
                                continue
                            existing.append(path)
                            seen.add(np)
                        media = existing

            return await original_execute(
                content,
                channel=channel,
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                buttons=buttons,
                **kwargs,
            )

        # Маркер для идемпотентности.
        _wrapped_execute._audit_track_attached = True  # type: ignore[attr-defined]
        message_tool.execute = _wrapped_execute  # type: ignore[method-assign]
        return True, "MessageTool.execute wrapped (auto-attach for message tool)"
