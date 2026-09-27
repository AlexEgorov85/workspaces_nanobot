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
import os
import sys as _sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _resolve_media_path(media_paths: list[str], basename: str) -> str:
    """Найти путь в ``media_paths`` по совпадению с ``basename``.

    Используется патчем ``patch_document_text_threshold`` для маркера
    ``read at <path>``: когда текст документа обрезан, агент должен
    иметь возможность прочитать файл сам. Возвращает первый путь,
    чей ``Path(p).name`` совпадает с ``basename`` (точное совпадение,
    без нормализации — имена файлов в проекте уникальны в пределах
    одного сообщения). Если совпадения нет — возвращает ``""``.
    """
    if not basename:
        return ""
    for p in media_paths or []:
        if not isinstance(p, str) or not p:
            continue
        try:
            if Path(p).name == basename:
                return p
        except (OSError, ValueError):
            continue
    return ""


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


@dataclass(frozen=True)
class PatchSpec:
    """Метаданные одного monkey-patch.

    Описывает ЗАЧЕМ патч существует, какой nanobot-API трогает, есть ли
    публичная альтернатива и какой риск при апгрейде nanobot. Нужен для
    audit-trail в ``PatchReport.details`` и для быстрой диагностики при
    обновлении nanobot-ai (см. TARGET §26).

    Attributes:
        name: короткое имя патча (ключ в ``apply_all``).
        purpose: человекочитаемое описание цели (1 строка).
        nanobot_target: какой API/модуль nanobot трогается
            (например, ``nanobot.agent.loop.AgentLoop._save_turn``).
        reason: почему это monkey-patch, а не использование публичного API.
        alternatives_checked: что проверяли перед тем, как делать patch
            (публичный API / hook / callback / config-ключ).
        risk: уровень риска при апгрейде (``low``/``medium``/``high``).
            ``high`` — патч трогает приватный метод, ломается при rename.
        nanobot_version: версия nanobot, на которой патч валидирован.
    """

    name: str
    purpose: str
    nanobot_target: str
    reason: str
    alternatives_checked: str
    risk: str
    nanobot_version: str = "0.3.0"


_PATCH_SPECS: dict[str, PatchSpec] = {
    "context_governor": PatchSpec(
        name="context_governor",
        purpose="выгружать большие результаты инструментов в data_store/ "
                "вместо заглушки обрезки",
        nanobot_target="nanobot.agent.context_governance.ContextGovernor"
                       ".normalize_tool_result",
        reason="nanobot режет вывод инструментов по умолчанию и теряет данные",
        alternatives_checked="config-ключи не покрывают кастомный persist-каталог",
        risk="medium",
    ),
    "save_turn": PatchSpec(
        name="save_turn",
        purpose="архивировать полные tool-результаты в data_store/ при "
                "сохранении истории оборота (вместо truncate в _save_turn)",
        nanobot_target="nanobot.agent.loop.AgentLoop._save_turn",
        reason="_save_turn — приватный метод; nanobot не имеет публичного "
               "extension point для кастомного persist",
        alternatives_checked="public hook 'before/after_save_turn' отсутствует",
        risk="high",
    ),
    "exec_limits": PatchSpec(
        name="exec_limits",
        purpose="сделать лимиты вывода exec-инструмента конфигурируемыми",
        nanobot_target="nanobot.agent.tools.exec_session.MAX_OUTPUT_CHARS, "
                       "shell.ExecTool._MAX_OUTPUT",
        reason="конфигурируемых лимитов вывода exec в nanobot нет; дефолт "
               "50K символов теряет данные",
        alternatives_checked="ToolConfig-схема параметров — обходится через "
                             "schema bump",
        risk="medium",
    ),
    "exec_timeout_cap": PatchSpec(
        name="exec_timeout_cap",
        purpose="поднять потолок таймаута exec (константа _MAX_TIMEOUT и "
                "схема параметра timeout) для долгих навыков вроде "
                "legal_summarizer",
        nanobot_target="nanobot.agent.tools.shell.ExecTool._MAX_TIMEOUT, "
                       "shell.ExecTool.parameters.timeout.maximum",
        reason="хардкод 600с убивал много-минутные прогоны, даже при "
               "exec_timeout=0, если агент передавал явный timeout",
        alternatives_checked="exec_timeout=0 в project.json снимает лимит, "
                             "но только когда агент НЕ передаёт timeout; "
                             "патч страхует случай явного timeout",
        risk="medium",
    ),
    "tool_limits": PatchSpec(
        name="tool_limits",
        purpose="сделать лимиты read_file/grep/list_dir конфигурируемыми",
        nanobot_target="nanobot.agent.tools.filesystem.ReadFileTool._MAX_CHARS, "
                       "ListDirTool._DEFAULT_MAX; "
                       "nanobot.agent.tools.search._DEFAULT_HEAD_LIMIT, "
                       "GrepTool._MAX_FILE_BYTES",
        reason="конфигурируемых лимитов read_file/grep/list_dir в nanobot нет",
        alternatives_checked="ToolConfig параметров не покрывает модульные константы",
        risk="medium",
    ),
    "assemble_outbound": PatchSpec(
        name="assemble_outbound",
        purpose="внедрить tool_audit и recent_files в финальный "
                "outbound (UI-метаданные для канала и CLI); "
                "context_window — отдельный путь (D7)",
        nanobot_target="nanobot.agent.loop.AgentLoop._assemble_outbound",
        reason="nanobot не имеет post-processor hook для OutboundMessage; "
               "_assemble_outbound — единственная точка финала; сигнатура "
               "изменилась в 0.3.5 (msg, final_content, stop_reason, "
               "streamed_content, *, log_content, turn_latency_ms) — "
               "обёртка следована под новые kwargs",
        alternatives_checked="AgentHook.finalize_content не получает "
                             "OutboundMessage; метрика context_window "
                             "вынесена в подписку TurnRuntimeAdmitted (D7)",
        risk="high",
    ),
    "context_bridge_seed": PatchSpec(
        name="context_bridge_seed",
        purpose="DEPRECATED в nanobot 0.3.5: _state_build удалён. "
                "Заменено подпиской bus.subscribe(TurnRuntimeAdmitted), "
                "регистрируется в ApplicationContext.start().",
        nanobot_target="nanobot.bus.queue.MessageBus.subscribe(TurnRuntimeAdmitted)",
        reason="DEPRECATED: _state_build — приватный метод, удалён в 0.3.5; "
               "TurnRuntimeAdmitted — публичный тип-агента, "
               "RuntimeEventPublisher публикует через bus.publish, "
               "что попадает в локальный fan-out для подписчиков.",
        alternatives_checked="bus.subscribe — публичный API; LLMRuntime."
                             "context_window_tokens — публичное поле.",
        risk="low",
        nanobot_version="0.3.0",
    ),
    "async_save": PatchSpec(
        name="async_save",
        purpose="вынести sessions.save из event-loop в executor, чтобы "
                "синхронный save не блокировал async-канал",
        nanobot_target="nanobot.agent.loop.AgentLoop.sessions.save",
        reason="nanobot вызывает sessions.save синхронно из async-методов; "
               "публичного async-API нет",
        alternatives_checked="AgentHook.after_run — слишком поздно",
        risk="medium",
    ),
    "subagent_logging": PatchSpec(
        name="subagent_logging",
        purpose="проксировать tool-события подагентов в DbLoggingService + "
                "персистить их историю",
        nanobot_target="nanobot.agent.subagent._SubagentHook",
        reason="_SubagentHook пишет только debug в loguru; БД-логирование "
               "подагентов отсутствует",
        alternatives_checked="AgentHook — не передаётся в AgentRunner.run() "
                             "subagent'а",
        risk="high",
    ),
"project_tools": PatchSpec(
        name="project_tools",
        purpose="auto-discover + регистрация пользовательских tool'ов из "
                "workspace/tools/*.py в AgentLoop.tools; DI-расширения "
                "(_agent_ref, _settings_ref, _cache_store_ref, "
                "_db_logging_service) — через setattr после конструктора "
                "(ToolContext frozen=False в 0.3.5)",
        nanobot_target="nanobot.agent.tools.base.Tool, ToolRegistry, "
                       "ToolContext, RuntimeControl",
        reason="nanobot не имеет механизма подключения пользовательских "
               "tool-каталогов; сигнатура ToolContext обновилась в 0.3.5: "
               "file_state_store сохранён, runtime_events удалён, "
               "добавлен runtime_control",
        alternatives_checked="ToolLoader.discover — ищет только во встроенных "
                             "пакетах",
        risk="low",
    ),
    "compact_tracking": PatchSpec(
        name="compact_tracking",
        purpose="DEPRECATED в nanobot 0.3.5: метод "
                "Consolidator.maybe_consolidate_by_tokens удалён; upstream "
                "AutoCompact.idle-ttl=0 уже short-circuit. Перенесено в "
                "CompactionEventSubscriber (см. design D1).",
        nanobot_target="nanobot.agent.autocompact.AutoCompact._archive, "
                       "nanobot.agent.memory.Consolidator"
                       ".maybe_consolidate_by_tokens",
        reason="DEPRECATED: оба метода обёрток заменены upstream-событием "
               "ContextCompactionEvent. Канал видит "
               "OutboundMessage.event и зовёт CompactionEventSubscriber "
               "(см. openspec/changes/nanobot-035-upgrade/design.md).",
        alternatives_checked="AgentHook.on_compact — не существует в nanobot "
                             "0.3.5; bus.subscribe не получит события "
                             "из publish_event (channel queue)",
        risk="low",
        nanobot_version="0.3.0",
    ),
    "compact_command": PatchSpec(
        name="compact_command",
        purpose="DEPRECATED в nanobot 0.3.5: upstream 'cmd_compact' "
                "(nanobot/command/builtin.py:348) уже делает нужное "
                "через loop.consolidator.compact_idle_session. Наш "
                "/compact не нужен — notify_session_compacted остаётся "
                "и зовётся из CompactionEventSubscriber (D1).",
        nanobot_target="nanobot.command.router.CommandRouter",
        reason="DEPRECATED: upstream builtin /compact покрывает сценарий; "
               "наш handler дублировал функционал.",
        alternatives_checked="Tool 'compact' — LLM решает вызывать или нет; "
                             "CompactionEventSubscriber (см. design.md §D1)",
        risk="low",
        nanobot_version="0.3.0",
    ),
    "idle_guard": PatchSpec(
        name="idle_guard",
        purpose="DEPRECATED в nanobot 0.3.5: upstream '_is_expired' "
                "(autocompact.py:39-55) уже short-circuit при _ttl<=0; "
                "ttl=0 в config.json проекта делает upstream-ветку "
                "бесполезной сама по себе.",
        nanobot_target="nanobot.agent.autocompact.AutoCompact.check_expired",
        reason="DEPRECATED: dead code в nanobot 0.3.5.",
        alternatives_checked="config 'idleCompactAfterMinutes: 0' — upstream "
                             "возвращает False из _is_expired; патч больше "
                             "не нужен",
        risk="low",
        nanobot_version="0.3.0",
    ),
    "session_content_cleanup": PatchSpec(
        name="session_content_cleanup",
        purpose="чистить невалидные символы (NUL, control-chars) из контента "
                "при Session.add_message",
        nanobot_target="nanobot.session.manager.Session.add_message",
        reason="add_message — единая точка, через которую в сессию попадают "
               "user/assistant/tool; NUL-байты валят запись в PostgreSQL",
        alternatives_checked="public sanitizer — отсутствует",
        risk="low",
    ),
    "document_text_threshold": PatchSpec(
        name="document_text_threshold",
        purpose="единый универсальный механизм встраивания документов в "
                "user-промпт (все каналы и навыки): заголовок каждого блока "
                "всегда содержит путь к файлу; при превышении порога тело "
                "заменяется на короткий маркер text omitted",
        nanobot_target="nanobot.utils.document.reference_non_image_attachments "
                       "(в nanobot 0.3.5 вместо удалённого extract_documents)",
        reason="в 0.3.5 extract_documents удалён; upstream по умолчанию "
               "вставляет только [Attachment: <path>], что заставляет LLM "
               "вызывать read_file даже для маленьких PDF/DOCX; патч "
               "читает текст через extract_text и встраивает его в "
               "content, защищая от раздувания контекста через порог",
        alternatives_checked="config 'channels.extractDocumentText=false' — "
                             "только полностью выключает извлечение, без "
                             "промежуточного режима «текст ≤ N»",
        risk="medium",
    ),
}


_SKIPPABLE_REASONS: frozenset[str] = frozenset({
    "agent is None",
    "persist_threshold <= 0",
    "exec_max_output_chars <= 0",
    "read_file_max_chars <= 0",
    "db_logging_service is None",
    "gateway.compact.enabled=false",
    "gateway.compact.notify_in_history=false",
    "workspace/tools not found — skip",
    "no project tools found",
    "agent.auto_compact is missing",
    "agent.commands is missing",
    "auto_compact is missing",
    "auto_compact.check_expired is missing",
    "agent.sessions is missing",
    "exec_session/shell module not loaded",
    "filesystem/search module not loaded",
    "gateway.runtime_diagnostics.session_dir_watch != true",
    "document_text_threshold <= 0",
})


def _classify_skip(detail: str) -> bool:
    """True, если причина — конфигуративный skip (а не реальный сбой).

    ``_record`` использует это, чтобы решить: деталь попадает в
    ``report.skipped`` или ``report.failed``. ``True`` = skip,
    ``False`` = failed.
    """
    if detail in _SKIPPABLE_REASONS:
        return True
    if detail.startswith("idle compact enabled"):
        return True
    if detail.startswith("no project tools found"):
        return True
    if detail.startswith("[INTERNAL_FAILED]"):
        return False
    return False


class PatchReport:
    """Отчёт о применении патчей: что применено / пропущено / упало.

    Состояния:
      * ``applied`` — патч успешно применён;
      * ``skipped`` — патч не применён **по конфигурации** (порог = 0,
        фича выключена и т.п.); это не дефект;
      * ``failed`` — патч пытался примениться, но не смог (изменился API
        nanobot, import error и т.п.); требует внимания.

    Все состояния (включая applied) сохраняют деталь в ``details`` —
    для дампа в startup-логе и для диагностики при апгрейде nanobot.
    """

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.details: dict[str, str] = {}

    def to_dict(self) -> dict:
        return {
            "applied": list(self.applied),
            "skipped": [list(t) for t in self.skipped],
            "failed": [list(t) for t in self.failed],
            "details": dict(self.details),
        }

    def render(self, *, specs: dict[str, PatchSpec] | None = None) -> str:
        """Человекочитаемая сводка для startup-диагностики.

        Формат:
            Runtime patches
            ----------------
            ✓ context_governor
            ✓ save_turn
            ⚠ idle_guard skipped: idle compact enabled (ttl=180)
            ✗ compact_tracking failed: import failed: ...

        При наличии ``specs`` добавляется строка ``(purpose: ...)`` под
        каждым failed, чтобы оператор сразу видел, зачем патч был нужен.
        """
        lines = ["Runtime patches", "-" * 16]
        for name in self.applied:
            lines.append(f"✓ {name}")
            if specs and name in specs:
                lines.append(f"    ({specs[name].purpose})")
        for name, detail in self.skipped:
            lines.append(f"⚠ {name} skipped: {detail}")
            if specs and name in specs:
                lines.append(f"    ({specs[name].purpose})")
        for name, detail in self.failed:
            lines.append(f"✗ {name} failed: {detail}")
            if specs and name in specs:
                lines.append(f"    ({specs[name].purpose})")
        return "\n".join(lines)


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
        cache_store: Any = None,
        bus: Any = None,
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
            cache_store: ``CacheProvider`` (для DI в generic tools через
                ``patch_project_tools``; ``None`` — патч пропускает DI,
                tool'ы остаются со своими fallback'ами).

        Returns:
            ``PatchReport`` со списками ``applied`` / ``skipped`` (с причиной).
        """
        report = PatchReport()
        self._record(report, "context_governor", self.patch_context_governor(
            config, settings, workspace_dir))
        self._record(report, "save_turn", self.patch_save_turn(
            settings, workspace_dir, agent))
        self._record(report, "exec_limits", self.patch_exec_limits(settings))
        self._record(report, "exec_timeout_cap", self.patch_exec_timeout_cap(settings))
        self._record(report, "tool_limits", self.patch_tool_limits(settings))
        self._record(report, "assemble_outbound", self.patch_assemble_outbound(
            agent, tool_audit_hook, recent_files_hook=recent_files_hook))
        self._record(report, "context_bridge_seed", self.patch_context_bridge_seed(agent))
        self._record(report, "async_save", self.patch_async_session_saves(agent))
        self._record(report, "session_dir_watch", self.patch_session_dir_watch(
            agent, workspace_dir))
        self._record(report, "subagent_logging", self.patch_subagent_logging(
            db_logging_service, session_manager, bus=bus))
        self._record(report, "project_tools", self.patch_project_tools(
            agent, workspace_dir, settings=settings,
            cache_store=cache_store, db_logging_service=db_logging_service))
        self._record(report, "document_text_threshold", self.patch_document_text_threshold(settings))
        self._record(report, "session_content_cleanup", self.patch_session_content_cleanup())
        return report

    @staticmethod
    def patch_specs() -> dict[str, PatchSpec]:
        """Метаданные всех зарегистрированных патчей.

        Используется в startup-логах (через ``PatchReport.render(specs=...)``)
        и при ручном аудите зависимости от nanobot. Ключи совпадают с
        ``name`` в ``PatchReport``.
        """
        return dict(_PATCH_SPECS)

    @staticmethod
    def _record(report: PatchReport, name: str, result: tuple[bool, str]) -> None:
        """Записать результат одного патча в ``PatchReport``.

        ``True`` → ``applied`` (если в detail нет маркера
        ``[INTERNAL_FAILED]``); ``False`` → ``skipped`` или ``failed``
        в зависимости от причины (``_classify_skip``).
        Маркер ``[INTERNAL_FAILED]`` в detail переклассифицирует
        успешный патч (например, ``patch_project_tools`` с частичным
        успехом) в ``failed``.
        """
        ok, detail = result
        report.details[name] = detail
        if ok and not detail.startswith("[INTERNAL_FAILED]"):
            report.applied.append(name)
            return
        if _classify_skip(detail):
            report.skipped.append((name, detail))
        else:
            report.failed.append((name, detail))

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
    # Патч 2b: DEPRECATED в nanobot 0.3.5 — перенесён в
    # ApplicationContext.start() через подписку на TurnRuntimeAdmitted.
    # ------------------------------------------------------------------

    def patch_context_bridge_seed(self, agent: Any) -> tuple[bool, str]:
        """DEPRECATED: в nanobot 0.3.5 ``_state_build`` удалён.

        Seed лимита окна/модели теперь подпиской на
        ``TurnRuntimeAdmitted`` регистрируется в
        ``ApplicationContext.start()`` (через
        ``bus.subscribe(handler, TurnRuntimeAdmitted)`` —
        ``bus/runtime_events.py:204`` использует ``bus.publish``, и
        подписка видит событие). Handler пишет
        ``runtime.context_window_tokens`` и ``runtime.model`` в мост
        ``DatabaseLoggingHook._CONTEXT_BRIDGE``.

        Метод оставлен в ``apply_all`` как no-op + ``applied`` для
        совместимости со ``PatchReport``/логами.
        """
        if agent is None:
            return False, "agent is None"
        return True, "context-bridge seed moved to bus.subscribe(TurnRuntimeAdmitted)"

    # ------------------------------------------------------------------
    # Патч 1: ContextGovernor.normalize_tool_result
    # ------------------------------------------------------------------

    def patch_context_governor(
        self, config: Any, settings: Any, workspace_dir: Any
    ) -> tuple[bool, str]:
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
    ) -> tuple[bool, str]:
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

        def _serialize(content: Any) -> str | None:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                try:
                    return json.dumps(content, ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    return None
            return None

        def _wrap(session, messages, skip, *, turn_latency_ms=None, summary_checkpoint=None, input_persisted_early=False):
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
            return original(
                session,
                archived,
                skip,
                turn_latency_ms=turn_latency_ms,
                summary_checkpoint=summary_checkpoint,
                input_persisted_early=input_persisted_early,
            )

        agent._save_turn = _wrap
        return True, "AgentLoop._save_turn patched for archiving"

    # ------------------------------------------------------------------
    # Патч 1b: санитизация контента на источнике (Session.add_message)
    # ------------------------------------------------------------------

    def patch_document_text_threshold(
        self, settings: Any
    ) -> tuple[bool, str]:
        """Единый универсальный механизм встраивания документов в user-промпт.

        ``nanobot.utils.document.reference_non_image_attachments`` —
        ЕДИНСТВЕННОЕ место, через которое upstream 0.3.5 формирует
        файловые блоки в ``content`` user-сообщения (для всех каналов —
        Postgres/Redis/websocket/streamlit). В 0.3.5
        ``extract_documents`` удалён, и каналы НЕ должны дублировать
        информацию о файле собственными хинтами
        вида ``[Attachment: … (saved at …)]``: иначе агент видит два
        параллельных указания «файл там-то» и поведение расходится
        между каналами.

        Upstream-выдача формата ``reference_non_image_attachments``
        (utils/document.py:681):

            ``[Attachment: <path>]``

        — только путь, без извлечения текста документа. Этого мало для
        маленьких документов (≤ порога): LLM вынуждена для каждого
        вызывать ``read_file``, теряя обороты. Поэтому патч оборачивает
        ``reference_non_image_attachments``: для каждого НЕ-изображения
        пытается прочитать файл через ``extract_text`` и встроить
        результат в content; если длина тела превышает ``threshold`` —
        заменяет тело на короткий маркер ``text omitted``, сохраняя
        путь в заголовке.

        Итоговый формат каждого файлового блока:

          * маленький документ (длина ≤ порога):
            ``[File: <basename> (saved at <path>)]\n<text>``;
          * большой документ (> порога):
            ``[File: <basename> (saved at <path>)]\n[text omitted (len=… > threshold=…)]``;
          * нечитаемый файл / изображение — fallback на upstream:
            ``[Attachment: <path>]``.

        Путь в заголовке присутствует ВСЕГДА, поэтому агент в любом
        случае знает, куда передать файл (skill, ``read_file``, ``exec``).

        Настройка читается из ``channels.document_text_threshold``
        (общая для всех каналов). Дефолт 20000 символов: средний
        договор/акт/раздел закона укладывается, длинные книги —
        обрезаются. ``<=0`` — патч пропускается (NO-OP, upstream
        ``reference_non_image_attachments`` остаётся без обёртки).

        Returns:
            ``(True, ...)`` при успехе;
            ``(False, <причина>)`` при отказе.
        """
        raw = _get(settings, "channels", "document_text_threshold", default=20000)
        try:
            threshold = int(raw)
        except (TypeError, ValueError):
            return False, "document_text_threshold is not an int"
        if threshold <= 0:
            return False, "document_text_threshold <= 0"

        try:
            from nanobot.utils import document as _document_mod
        except Exception as exc:
            return False, f"import failed: {exc}"

        reference = getattr(_document_mod, "reference_non_image_attachments", None)
        if reference is None:
            return False, "reference_non_image_attachments is missing"

        extract_text = getattr(_document_mod, "extract_text", None)
        is_image_file = getattr(_document_mod, "is_image_file", None)

        try:
            def _is_image(path: str) -> bool:
                if is_image_file is None or not isinstance(path, str):
                    return False
                try:
                    return bool(is_image_file(path))
                except Exception:
                    return False

            def _read_attachment_body(path: str) -> str | None:
                """Прочитать текст вложения через ``extract_text``.

                Возвращает ``None`` для изображений, нечитаемых форматов
                или ошибок чтения — в этом случае обёртка оставляет
                upstream-маркер ``[Attachment: <path>]``.
                """
                if not isinstance(path, str) or not path:
                    return None
                if _is_image(path):
                    return None
                if extract_text is None:
                    return None
                try:
                    text = extract_text(path)
                except Exception:
                    return None
                if not isinstance(text, str) or not text:
                    return None
                return text

            def _build_block(path: str) -> str:
                """Сформировать файловый блок с учётом порога.

                ``[File: <basename> (saved at <path>)]\n<body>``
                либо ``[Attachment: <path>]`` (fallback).
                """
                basename = Path(path).name or path
                body_text = _read_attachment_body(path)
                if body_text is None:
                    return f"[Attachment: {path}]"
                body_len = len(body_text)
                header = f"[File: {basename} (saved at {path})]"
                if body_len <= threshold:
                    return f"{header}\n{body_text}"
                return (
                    f"{header}\n"
                    f"[text omitted (len={body_len} > threshold={threshold})]"
                )

            def _reference_with_threshold(
                content: str, media: list[str],
            ) -> tuple[str, list[str]]:
                media_list = list(media or [])
                image_paths: list[str] = []
                attachment_blocks: list[str] = []
                for path in media_list:
                    if _is_image(path):
                        image_paths.append(path)
                    else:
                        attachment_blocks.append(_build_block(path))
                if attachment_blocks:
                    suffix = "\n\n".join(attachment_blocks)
                    content = f"{content}\n\n{suffix}" if content else suffix
                return content, image_paths

            _document_mod.reference_non_image_attachments = (
                _reference_with_threshold
            )

            return (
                True,
                "reference_non_image_attachments patched for document_text_threshold",
            )
        except Exception as exc:
            return False, f"patch failed: {exc}"

    def patch_session_content_cleanup(self) -> tuple[bool, str]:
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

    def patch_async_session_saves(self, agent: Any) -> tuple[bool, str]:
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

    # ------------------------------------------------------------------
    # Патч 1d-bis: диагностическое логирование пропавшего sessions_dir
    # ------------------------------------------------------------------

    def patch_session_dir_watch(
        self, agent: Any, workspace_dir: Any
    ) -> tuple[bool, str]:
        """Снять показания вокруг ``SessionManager.save`` для расследования.

        Временный диагностический патч: ошибки вида
        ``FileNotFoundError: ...sessions/<key>.jsonl.tmp`` на ``open("w")``
        означают, что ``self.sessions_dir`` исчез между конструктором
        ``SessionManager`` и моментом ``save``. Чтобы подтвердить или
        опровергнуть гипотезу, оборачиваем ``save`` так, чтобы он:

          * непосредственно перед делегированием в ``original`` фиксировал
            наличие ``self.sessions_dir`` (через ``is_dir()`` + ``stat().st_mtime``);
          * при ошибке ``FileNotFoundError`` в ``open(tmp_path, "w")`` логировал
            полную картину: ``self.sessions_dir``, ``tmp_path.parent``,
            ``os.getcwd()``, ``os.listdir(self.sessions_dir.parent)`` (если
            parent существует) — этого достаточно, чтобы понять, удалили
            папку, переименовали workspace, или проблема в антивирусе.

        Поведение ``save`` НЕ меняется: мы только читаем состояние ДО вызова
        и логируем при ошибке. Никаких ``mkdir``, никаких повторов.

        Гейт: запускается только если в ``settings.gateway.runtime_diagnostics``
        есть ``session_dir_watch: true``. По умолчанию выключено — патч не
        нужен в проде, только для расследования.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        try:
            import config as _config
            full_settings = getattr(_config, "SETTINGS", None)
            diagnostics = (
                (full_settings.get("gateway", {}) or {}).get("runtime_diagnostics", {})
                if full_settings is not None else {}
            )
        except Exception:
            diagnostics = {}

        if not diagnostics.get("session_dir_watch"):
            return False, "gateway.runtime_diagnostics.session_dir_watch != true"

        if agent is None:
            return False, "agent is None"
        sessions = getattr(agent, "sessions", None)
        if sessions is None:
            return False, "agent.sessions is missing"
        original = getattr(sessions, "save", None)
        if original is None:
            return False, "agent.sessions.save is missing"
        if getattr(sessions, "_session_dir_watch_patched", False):
            return False, "already patched"

        sessions_dir = getattr(sessions, "sessions_dir", None)

        def _snapshot_state() -> dict[str, Any]:
            try:
                exists = bool(sessions_dir.is_dir()) if sessions_dir is not None else False
            except OSError as exc:
                return {"sessions_dir": str(sessions_dir), "is_dir_error": repr(exc)}
            try:
                mtime = sessions_dir.stat().st_mtime if exists else None
            except OSError as exc:
                mtime = f"stat_error:{exc!r}"
            return {
                "sessions_dir": str(sessions_dir),
                "exists": exists,
                "mtime": mtime,
            }

        def _wrapped_save(session: Any, fsync: bool = False) -> Any:
            pre = _snapshot_state()
            try:
                return original(session, fsync=fsync)
            except FileNotFoundError as exc:
                post = _snapshot_state()
                try:
                    parent_listing = (
                        sorted(os.listdir(str(sessions_dir.parent)))
                        if sessions_dir is not None and sessions_dir.parent.exists()
                        else None
                    )
                except OSError as exc2:
                    parent_listing = f"listdir_error:{exc2!r}"
                logger.error(
                    "session_dir_watch: FileNotFoundError на save: "
                    "sessions_dir.exists={pre_exists}->{post_exists}, "
                    "cwd={cwd}, parent_listing={parent_listing}, "
                    "session_key={key}, "
                    "original_error={err!r}",
                    pre_exists=pre.get("exists"),
                    post_exists=post.get("exists"),
                    cwd=os.getcwd(),
                    parent_listing=parent_listing,
                    key=getattr(session, "key", "<unknown>"),
                    err=exc,
                )
                raise

        sessions.save = _wrapped_save
        sessions._session_dir_watch_patched = True
        return True, "agent.sessions.save wrapped with diagnostic logging"

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

    def patch_exec_limits(self, settings: Any) -> tuple[bool, str]:
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
            # ``WriteStdinTool`` удалён в nanobot 0.3.5 — guard через hasattr.
            ws_tool = getattr(es, "WriteStdinTool", None)
            if ws_tool is not None:
                self._bump_schema_max(
                    ws_tool, ("max_output_chars", "max_output_tokens"), max_out
                )
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, "exec output limits patched"

    # ------------------------------------------------------------------
    # Патч 1c-2: потолок таймаута exec (константа + схема параметра)
    # ------------------------------------------------------------------

    def patch_exec_timeout_cap(self, settings: Any) -> tuple[bool, str]:
        """Поднять хардкод-потолок таймаута exec выше 600 сек.

        nanobot жёстко ограничивает per-call таймаут ``_MAX_TIMEOUT = 600``
        (``shell.py:247``) и схемой параметра ``timeout`` (``maximum=600``).
        Для долгих навыков (legal_summarizer: 7–10 мин на ГК РФ) это убивало
        прогон, даже при ``exec_timeout=0`` в project.json, если агент передавал
        явный ``timeout`` (TOOLS.md учит передавать таймаут). Патч поднимает
        оба потолка до ``gateway.exec_timeout_cap_sec`` (дефолт 3600).

        Полностью безлимитной сессия становится при ``exec_timeout=0`` И когда
        агент НЕ передаёт ``timeout`` (см. SKILL.md legal_summarizer) — тогда
        ``_resolve_timeout`` возвращает ``None`` и deadline = inf. Патч лишь
        расширяет коридор для явного ``timeout``.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` при отказе.
        """
        cap = int(_get(settings, "gateway", "exec_timeout_cap_sec", default=3600) or 3600)
        if cap <= 0:
            return False, "exec_timeout_cap_sec <= 0"

        try:
            shell = _getloaded("nanobot.agent.tools.shell")
            if shell is None:
                return False, "shell module not loaded"
            if not hasattr(shell.ExecTool, "_MAX_TIMEOUT"):
                return False, "ExecTool._MAX_TIMEOUT not found"

            shell.ExecTool._MAX_TIMEOUT = cap
            # Схема параметра timeout: снять потолок 600, иначе агент не сможет
            # запросить больше и явный timeout всё равно упрётся в 600.
            self._bump_schema_max(shell.ExecTool, ("timeout",), cap)
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, f"exec timeout cap raised to {cap}s"

    # ------------------------------------------------------------------
    # Патч 1d: лимиты read_file / grep / list_dir (конфигурируемые)
    # ------------------------------------------------------------------

    def patch_tool_limits(self, settings: Any) -> tuple[bool, str]:
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
    ) -> tuple[bool, str]:
        """Подменить ``agent._assemble_outbound`` обёрткой, дописывающей аудит.

        Сигнатура upstream ``AgentLoop._assemble_outbound`` в nanobot 0.3.5:

            ``(self, msg, final_content, stop_reason, streamed_content,
               *, log_content=True, turn_latency_ms=None) -> OutboundMessage | None``

        ``_dispatch`` зовёт метод с этими позиционными аргументами + kwarg
        ``log_content``. Обёртка вызывает оригинальный метод as-is и
        дописывает:

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
            basename.

        ``context_window`` (метрика занятости окна) живёт в мосте
        ``DatabaseLoggingHook._CONTEXT_BRIDGE`` и обновляется подпиской
        на ``TurnRuntimeAdmitted``/обращениями к
        ``get_context_window(session_key)``. Эта обёртка только
        фиксирует блок в ``_store_context_window`` через
        ``_attach_context_window`` в момент финала.

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

        def _wrap(msg, final_content, stop_reason, streamed_content,
                  *, log_content=True, turn_latency_ms=None):
            from lib.utils.outbound_meta import FINAL_TURN_KEY as _FINAL_TURN
            result = original(
                msg, final_content, stop_reason, streamed_content,
                log_content=log_content, turn_latency_ms=turn_latency_ms,
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
        self,
        db_logging_service: Any,
        session_manager: Any = None,
        *,
        bus: Any = None,
    ) -> tuple[bool, str]:
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
            from lib.hooks.database_logging_hook import _usage_to_dict
        except Exception as exc:
            return False, f"import failed: {exc}"

        class _SubagentLoggingHook(_SubagentHook):
            """_SubagentHook + БД-логирование + персист истории подагента."""

            _sessions = session_manager
            _default_bus: Any = None

            @classmethod
            def set_default_bus(cls, bus: Any) -> None:
                """Установить bus для автопривязки к новым инстансам.

                Используется ``RuntimeEventsSubscriber`` (см.
                ``lib/services/runtime_events_subscriber.py``) при
                подписке на ``SubagentTurnCompleted``. После установки
                каждый новый ``_SubagentLoggingHook`` инстанс будет
                автоматически получать ``self._bus = bus``, и его
                ``_publish_subagent_turn_completed`` будет эмитить
                события в ``bus``.

                См. openspec/changes/post-0.3.5-patches-cleanup/design.md D3.
                """
                cls._default_bus = bus

            def __init__(self, task_id, status=None, bus=None):
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
                # MessageBus для публикации SubagentTurnCompleted.
                # 1) Явный параметр ``bus`` (предпочтительно для прямых
                # вызовов из тестов).
                # 2) Fallback: берём class-level state, который
                # RuntimeEventsSubscriber может установить через
                # ``_SubagentLoggingHook.set_default_bus(bus)``
                # (см. lib/services/runtime_events_subscriber.py).
                # Если None — публикация пропускается, subagent_run_finished
                # пишется через _finalize как раньше (backward compat).
                # См. openspec/changes/post-0.3.5-patches-cleanup/design.md D3.
                self._bus = bus if bus is not None else getattr(
                    _SubagentLoggingHook, "_default_bus", None
                )

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
                # ``user_id`` родителя — security boundary для
                # ``history_search(session_scope="all")``. Subagent не имеет
                # собственного identity-store (его session_key =
                # subagent:<task_id>); без явного прокидывания индекс для
                # subagent-сессии был бы заполнен ``user_id=None`` и события
                # подагента не попадали бы в ``scope='all'`` пользователя.
                # Прокидываем user_id родителя явно: register_request
                # кладёт пару {request_id, user_id} в индекс, и дальнейшие
                # tool/event-события подагента получают user_id через
                # request_id matching в ``_enqueue``.
                parent_user_id = self._resolve_parent_user_id(context)
                key = self._subagent_session_key(context)
                self._db_hook._service.register_request(
                    key,
                    self._session_id,   # request_id подагента = subagent:<task_id>
                    user_id=parent_user_id,
                    parent_request_id=self._parent_rid,
                    agent_id=self._session_id,
                    parent_agent_id=self._db_hook._agent_id,
                    is_subagent=True,
                    status="running",
                )

            def _resolve_parent_user_id(self, context) -> str | None:
                """Получить ``user_id`` родительского request.

                Источники (по приоритету):
                  1. ``RequestContext.sender_id`` текущего request (если
                     subagent вызван внутри нормального оборота и контекст
                     доступен) — это та же identity, что попадает в
                     ``agent_question_runs.user_id`` родителя.
                  2. ``None`` (нет identity-store) — события подагента
                     пишутся с ``user_id IS NULL`` и НЕ попадают в
                     ``scope='all'`` (безопасный default).

                Никаких fallback'ов на другие поля — отсутствие identity =
                жёсткий отказ.
                """
                try:
                    from nanobot.agent.tools.context import current_request_context
                except Exception:
                    return None
                try:
                    ctx = current_request_context()
                except Exception:
                    return None
                if ctx is None:
                    return None
                sender_id = getattr(ctx, "sender_id", None)
                if isinstance(sender_id, str) and sender_id:
                    return sender_id
                return None

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
                await self._publish_subagent_turn_completed(context, had_error=False)
                await self._finalize(context)

            async def on_error(self, context):
                # runner вызывает on_error до after_run в путях с error —
                # guard-флаг исключает двойную запись истории/итога
                await self._publish_subagent_turn_completed(context, had_error=True)
                await self._finalize(context)

            async def _publish_subagent_turn_completed(
                self, context, *, had_error: bool
            ):
                """Опубликовать кастомный SubagentTurnCompleted через
                ``bus.publish(event)``.

                Используется ``RuntimeEventsSubscriber`` (см.
                ``lib/services/runtime_events_subscriber.py``) для записи
                ``subagent_run_finished`` в ``agent_gateway_logs`` через
                нативный pub-sub, заменяя прямое обращение к
                ``DbLoggingService`` из ``_finalize``.

                Если ``self._bus is None`` (нет шины — backward compat) —
                no-op. Запись в БД в этом случае остаётся за ``_finalize``.
                См. openspec/changes/post-0.3.5-patches-cleanup/design.md D3.
                """
                if self._bus is None:
                    return
                try:
                    from lib.events.subagent import SubagentTurnCompleted
                except Exception:
                    return

                final = getattr(context, "final_content", "") or ""
                tools = list(getattr(context, "tools_used", None) or [])
                stop_reason = getattr(context, "stop_reason", None)
                usage = getattr(context, "usage", None)
                error_text = getattr(context, "error", None) or None

                try:
                    task_text = self._extract_task(context) if hasattr(self, "_extract_task") else None
                except Exception:
                    task_text = None

                parent_user_id = self._resolve_parent_user_id(context)

                event = SubagentTurnCompleted(
                    task_id=self._task_id,
                    parent_request_id=self._parent_rid,
                    parent_user_id=parent_user_id,
                    final_content=final,
                    tools_used=tools,
                    stop_reason=stop_reason,
                    request_id=self._session_id,
                    task=task_text,
                    usage=usage,
                    had_error=bool(had_error),
                    error=error_text if had_error else None,
                )
                try:
                    publish = getattr(self._bus, "publish", None)
                    if publish is None:
                        return
                    result = publish(event)
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.warning(
                        "_SubagentLoggingHook.publish(SubagentTurnCompleted) failed: %s",
                        exc,
                    )

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
                    # Явный user_id родителя: security boundary для
                    # ``history_search(session_scope="all")``. _ensure_request
                    # уже обновил индекс, и request_id matching в _enqueue
                    # подставит user_id; явное значение гарантирует, что
                    # событие не зависит от состояния индекса (если между
                    # _ensure_request и _enqueue кто-то успел переписать
                    # индекс под другой request — explicit value всё равно
                    # побеждает согласно правилам _enqueue).
                    parent_user_id = self._resolve_parent_user_id(context)
                    self._db_hook._service.log_event(LogEvent(
                        event_type="subagent_run_finished",
                        level="ERROR" if context.error else "INFO",
                        session_id=self._session_id,
                        channel="subagent",
                        actor="agent",
                        name=self._task_id,
                        request_id=self._session_id,
                        user_id=parent_user_id,
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
                            "tokens_used": (
                                _usage_to_dict(getattr(context, "usage", None)) or {}
                            ).get("total_tokens"),
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
            def _extract_task(context) -> str | None:
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
        cache_store: Any = None,
        db_logging_service: Any = None,
    ) -> tuple[bool, str]:
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
                runtime_control=getattr(agent, "_runtime_control", None),
            )
            # ``agent`` не входит в ToolContext по контракту nanobot — кладём
            # отдельным атрибутом, чтобы tool'ы с DI-сервисами (например,
            # CompactContextTool) могли его получить через
            # ``getattr(ctx, "_agent_ref", None)``.
            ctx._agent_ref = agent
            if settings is not None:
                ctx._settings_ref = settings
            if cache_store is not None:
                ctx._cache_store_ref = cache_store
            if db_logging_service is not None:
                ctx._db_logging_service = db_logging_service
            if db_logging_service is not None:
                ctx._db_logging_service = db_logging_service

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
                    # DI: проброс инфраструктуры в tool'ы, которые её ожидают.
                    # Generic-путь ``set_provider`` / ``set_connection_factory``:
                    # если tool ожидает ``CacheProvider``/``cache_store`` —
                    # передаём реализацию из runtime, а не дефолтный fallback.
                    if cache_store is not None:
                        if hasattr(tool, "set_provider"):
                            try:
                                tool.set_provider(cache_store)
                            except Exception:
                                logger.exception(
                                    "set_provider failed for {}", cls.__name__,
                                )
                        elif hasattr(tool, "set_connection_factory"):
                            try:
                                tool.set_connection_factory(
                                    getattr(cache_store, "get_duckdb_connection", None)
                                    or getattr(cache_store, "connect", None)
                                )
                            except Exception:
                                logger.exception(
                                    "set_connection_factory failed for {}",
                                    cls.__name__,
                                )
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
            # Помечаем detail маркером ``[INTERNAL_FAILED]`` если внутри
            # ``for cls in candidates`` хоть один tool упал на
            # ``cls.enabled``/``cls.create``/``register``. Тогда
            # ``_record`` классифицирует этот патч как failed, а не
            # skipped (по умолчанию ``True`` → ``applied``).
            if failed:
                detail = "[INTERNAL_FAILED] " + detail
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
    # Вспомогательные методы для auto-compact/context-bridge УДАЛЕНЫ в 0.3.5
    # ------------------------------------------------------------------

    # В nanobot 0.3.5:
    # - ``Consolidator.maybe_consolidate_by_tokens`` отсутствует —
    #   token-budget компакция идёт через ``ContextCompactionEvent``
    #   (``nanobot.events.ContextCompactionEvent``);
    # - upstream ``cmd_compact`` (nanobot/command/builtin.py:348) покрывает
    #   ручной ``/compact``;
    # - upstream ``AutoCompact._is_expired`` уже short-circuit при
    #   ``_ttl <= 0``;
    # - upstream ``TurnRuntimeAdmitted`` идёт через ``bus.publish``
    #   и доступен через ``bus.subscribe``.
    #
    # Поэтому патчи ``compact_tracking``, ``compact_command``,
    # ``idle_guard`` удалены; обязанности перенесены в
    # ``lib/services/compaction_event_subscriber.py`` (D1) и
    # подписку на ``TurnRuntimeAdmitted`` (D7).
    # ------------------------------------------------------------------
