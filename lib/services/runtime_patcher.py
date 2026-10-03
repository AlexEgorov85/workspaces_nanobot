"""RuntimePatcher — ВСЕ monkey-patch'и к фреймворку nanobot в одном месте.

Устраняет дублирование между gateway.py и cli_agent.py:

  1. ``patch_subagent_logging`` — БД-логирование подагентов: их tool-события,
     итог запуска (``agent.completed``) и история пишутся в
     ``DbLoggingService`` и ``session_manager`` (SubagentManager использует
     внутренний ``_SubagentHook``, который иначе пишет только debug в loguru).

Фаза 6 (``enterprise-mcp-platform``) вынесла шесть патчей из этого модуля в
нативные точки расширения — здесь их больше нет, и второй реализации
механизма тоже нет:

  * ``save_turn`` → upstream
    ``nanobot.utils.helpers.maybe_persist_tool_result``;
  * ``document_text_threshold`` → ``workspace/tools/document_read.py``
    (нативный tool, порог в его собственном коде);
  * ``session_content_cleanup`` → ``SanitizingSessionStore.save``
    (``lib/session/pg_session_manager.py``);
  * ``async_save`` → обёртка в ``lib/services/session_storage.py``;
  * ``session_dir_watch`` → удалён целиком (диагностика расследована);
  * ``turn_delivery_fail`` → ``lib/services/turn_delivery_factory.py``
    (``FallbackTurnDeliveryFactory`` через публичный параметр
    ``AgentLoop(turn_delivery_factory=...)``);
  * ``assemble_outbound`` — оставлен: в nanobot 0.3.5 нет события конца
    оборота, на которое можно перевести ``_final_turn``/``media``, см.
    ``docs/architecture/runtime-patcher-inventory.md`` и ADR
    ``docs/architecture/decisions/turn-delivery-public-extension.md``.

Решение владельца сняло ещё два патча — ``exec_limits`` и ``tool_limits``.
Оба поднимали потолки вывода инструментов, которых в nanobot 0.3.5 нет в
конфигурации, поэтому нативной замены нет by design: потолки возвращены к
дефолтам библиотеки, а секция ``gateway.tool_result_limits`` из ``config.json``
удалена как мёртвая. Что это означает на практике (проверено на
установленном пакете 0.3.5):

  * вывод ``exec``: дефолт ``ExecTool._MAX_OUTPUT`` 100 000 → **10 000**,
    потолок ``MAX_OUTPUT_CHARS`` 500 000 → **50 000**. Усечение
    «голова + хвост» при этом не просто форматируется, а режется уже в
    ``_BoundedOutputBuffer``, то есть попавший в файл персиста вывод
    середину уже не содержит;
  * ``read_file``: ``ReadFileTool._MAX_CHARS`` 512 000 → **128 000**
    (обрезается хвост, маркер «(Showing lines …)» остаётся);
  * ``list_dir``: ``ListDirTool._DEFAULT_MAX`` 500 → **200**;
  * ``grep``: ``GrepTool._MAX_FILE_BYTES`` 20 МБ → **2 МБ** — файлы крупнее
    пропускаются целиком и считаются в ``skipped_large``, то есть детектор
    молчит, а модель видит пустой результат как факт.

Оставшийся ``exec_timeout_cap`` держит отдельный коридор и к этому
отношения не имеет.

``context_governor`` — тоже ушёл в upstream (change
``use-upstream-tool-result-persist``). В nanobot 0.3.5
``ContextGovernor.normalize_tool_result``
(``nanobot/agent/context_governance.py:709-759``) делает построчно то же,
что делал патч: ``ensure_nonempty_tool_result`` → исключение для
``read_file`` (``TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS``, строка 69) →
``maybe_persist_tool_result`` с публично настраиваемым порогом
``agents.defaults.max_tool_result_chars`` (``config/schema.py:131``).
Наш патч, хук ``ToolResultArchiveHook`` и upstream писали один и тот же
результат в три разных места тремя разными маркерами; остаётся ровно
одна реализация — библиотечная.

Регистрация кастомных tool'ов из ``workspace/tools/*.py`` — в отдельном
loader'е: ``lib/services/project_tool_loader.py::register_project_tools``;
вызывается из ``ApplicationContext.create()`` сразу после
``apply_all()``. ``RuntimePatcher`` НЕ зависит от loader'а.

Каждый патч — в try/except: если API nanobot изменился, патч не применяется,
процесс не падает, причина попадает в ``PatchReport``.
"""

from __future__ import annotations

import json
import sys as _sys
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


# ---------------------------------------------------------------------------
# Константы fallback'а (openspec/specs/runtime/error-fallback) переехали
# вместе с патчем в ``lib/services/turn_delivery_factory.py``: настраиваемый
# ответ на internal-ошибку теперь собирает фабрика, внедряемая публичным
# параметром ``AgentLoop(turn_delivery_factory=...)``.
# ---------------------------------------------------------------------------


class ContextWindowNotSeededError(RuntimeError):
    """``DatabaseLoggingContextBridge`` не засеян для ``session_key``.

    Bridge seed'ит ``TurnRuntimeAdmitted``-подписка из
    ``RuntimeEventsSubscriber.start()``. Если подписчик не активен
    (например, в standalone-тестах без ``ApplicationContext``) —
    блок ``metadata.context_window`` невозможно построить корректно.

    Это явная ошибка вместо тихого fallback'а на ``agent._last_usage``,
    который скрывал дефекты подписки. UI/CLI получает
    ``ContextWindowNotSeededError`` через exception-chain, и метрика
    НЕ отображается — это лучше, чем ``used=0``.
    """


def _attach_context_window(agent: Any, session_key: str, result: Any) -> None:
    """Внедрить ``metadata["context_window"]`` в финальный outbound.

    Метрика M1 (занятость окна): ``prompt_tokens`` последней итерации
    оборота (свежий по-итерационный usage из моста ``DatabaseLoggingHook``)
    поделённый на лимит окна модели.

    Источник истины — ``DatabaseLoggingContextBridge``, засевается
    через подписку на ``TurnRuntimeAdmitted`` в
    ``RuntimeEventsSubscriber.start()`` (см. design.md D5 opencode
    change post-0.3.5-patches-cleanup):

    * Bridge MUST содержать ``limit``/``model`` к моменту первого
      outbound'а (подписка заполняет через
      ``seed_context_window(session_key, limit, model)``).
    * ``usage`` пишется через ``_store_iteration_usage`` в
      ``DatabaseLoggingHook.after_iteration``.

    ``agent.context_window_tokens`` и ``agent.model`` — fallback
    (для unit-тестов с MagicMock, где bridge может быть засеян,
    но атрибуты агента не установлены). Если bridge пуст И атрибуты
    пусты — поднимается ``ContextWindowNotSeededError`` (явная
    ошибка вместо тихого fallback на ``agent._last_usage``, который
    скрывал дефекты подписки).

    Готовый блок дополнительно кладём в мост: канал читает его в фоновом
    цикле живого обновления и пишет в processing-строку ТОЛЬКО блок (без
    лимита — лимит знает только агент).
    """
    from lib.hooks.database_logging_hook import (
        _CONTEXT_BRIDGE,
        _CONTEXT_BRIDGE_LOCK,
        _store_context_window,
        get_iteration_usage,
    )
    usage = get_iteration_usage(session_key)

    # Лимит: bridge → agent. Если bridge засеян (подписка работает),
    # limit берётся из bridge. Если нет — fallback на
    # agent.context_window_tokens (для unit-тестов с MagicMock).
    bridge_limit = 0
    bridge_model = ""
    with _CONTEXT_BRIDGE_LOCK:
        bridge_entry = dict(_CONTEXT_BRIDGE.get(session_key) or {})
    if isinstance(bridge_entry, dict):
        bridge_limit = int(bridge_entry.get("limit") or 0)
        bridge_model = (
            bridge_entry.get("model", "")
            if isinstance(bridge_entry.get("model"), str)
            else ""
        )

    agent_limit = getattr(agent, "context_window_tokens", None) or 0
    if isinstance(agent_limit, bool) or not isinstance(agent_limit, int):
        agent_limit = 0

    limit = bridge_limit or agent_limit
    model = bridge_model or (getattr(agent, "model", None) or "")
    if isinstance(model, str) is False:
        model = ""

    if limit <= 0:
        # Ни bridge, ни agent не дают лимит — это явная ошибка,
        # не тихий used=0.
        raise ContextWindowNotSeededError(
            f"context_window not seeded for session_key={session_key!r}; "
            f"RuntimeEventsSubscriber.start() required before "
            f"_attach_context_window"
        )

    raw_used = (
        (usage or {}).get("prompt_tokens")
        if isinstance(usage, dict)
        else None
    )
    try:
        used = int(raw_used or 0)
    except (TypeError, ValueError):
        used = 0
    if used <= 0:
        # usage ещё не пришёл — первая итерация без tool-calls.
        # Допустимый случай: НЕ throw, просто used=0 показывает
        # клиенту «пока ничего не занято». Bridge засеян (limit > 0),
        # поэтому подписка работает.
        used = 0
    block = {
        "used": used,
        "limit": int(limit),
        "pct": round(min(1.0, used / float(limit)), 4) if limit > 0 else 0.0,
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
        required: критичность для diagnostics (НЕ для startup abort).
            ``True`` — failed/missing patch этого имени подсвечивается
            в startup-баннере через ``_emit_patch_inventory_banner`` и
            в ``diff_runtime_patches`` как ``missing_required`` /
            ``failed_required``. Это **только** metadata — control flow
            НЕ зависит от ``required``: failed-патч (включая
            ``required=True``) логируется warning'ом и
            ``ApplicationContext.create()`` продолжает работу.
            ``False`` (по умолчанию) — opt-in фича, skip по конфигу.
    """

    name: str
    purpose: str
    nanobot_target: str
    reason: str
    alternatives_checked: str
    risk: str
    nanobot_version: str = "0.3.0"
    required: bool = False


_PATCH_SPECS: dict[str, PatchSpec] = {
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
        required=True,
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
        required=True,
    ),
    "repeat_guard_block": PatchSpec(
        name="repeat_guard_block",
        purpose="превратить отказ защитника от повторов (mode=block) в "
                "синтетический результат инструмента вместо падения оборота",
        nanobot_target="nanobot.agent.tools.execution._execute_tool_call",
        reason="hook.before_execute_tool вызывается ДО try-блока "
               "(execution.py:165 против 166), поэтому RepeatGuardBlocked "
               "уходит из execute_tool_calls и валит весь оборот; модель не "
               "получает результата и не может попробовать иначе",
        alternatives_checked="return-значение из before_execute_tool "
                             "не поддерживается Hook-API; правка ctx."
                             "tool_calls не годится — runner копирует список "
                             "(runner.py:483) и передаёт оригинал; "
                             "агентный monkey-patch шире не нужен",
        risk="low",
    ),
}


_SKIPPABLE_REASONS: frozenset[str] = frozenset({
    # Конфигуративный skip: патч сознательно не применился, это НЕ сбой.
    # Отсутствие upstream-атрибута сюда НЕ входит намеренно: это дрейф API
    # библиотеки, и он обязан попадать в ``failed``, чтобы баннер запуска
    # его показал. Закреплено tests/test_runtime_patcher.py::
    # TestPatchReportClassification::test_missing_attr_is_failed.
    #
    # Записи ``exec_max_output_chars <= 0``, ``read_file_max_chars <= 0``,
    # ``exec_session/shell module not loaded`` и
    # ``filesystem/search module not loaded`` удалены вместе с патчами
    # ``exec_limits`` и ``tool_limits`` — больше их никто не выдаёт.
    "agent is None",
    "exec_timeout_cap_sec <= 0",
    "db_logging_service is None",
    "shell module not loaded",
})


def _classify_skip(detail: str) -> bool:
    """True, если причина — конфигуративный skip (а не реальный сбой).

    ``_record`` использует это, чтобы решить: деталь попадает в
    ``report.skipped`` или ``report.failed``. ``True`` = skip,
    ``False`` = failed.
    """
    if detail in _SKIPPABLE_REASONS:
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
            ✓ assemble_outbound
            ⚠ exec_timeout_cap skipped: exec_timeout_cap_sec <= 0
            ✗ subagent_logging failed: DbLoggingService is None

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


def _resolve_agent_id(config: Any, agent: Any) -> str | None:
    """Резолв идентификатора активного агента для передачи в патчи.

    Источники по убыванию приоритета:
    1. ``config.agents.defaults.name`` (если задано явно) или
       ``config.agents.defaults`` (default-агент).
    2. ``config.default_agent`` (если есть).
    3. ``agent.name`` (fallback на переданный ``AgentLoop``).
    4. ``None`` если ничего не удалось достать.

    nanobot ``Config`` (``config/schema.py:422``) хранит агентов в
    ``config.agents.defaults`` (один имплицитный default). Имя может быть
    задано явно или выводится из конфига; для runtime-событий проекта
    используется значение ``config.agents.defaults.name`` или
    ``"default"``.
    """
    try:
        defaults = getattr(getattr(config, "agents", None), "defaults", None)
        if defaults is not None:
            name = getattr(defaults, "name", None)
            if isinstance(name, str) and name:
                return name
    except Exception:
        pass
    try:
        default_agent = getattr(config, "default_agent", None)
        if isinstance(default_agent, str) and default_agent:
            return default_agent
    except Exception:
        pass
    try:
        agent_name = getattr(agent, "name", None)
        if isinstance(agent_name, str) and agent_name:
            return agent_name
    except Exception:
        pass
    return None


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
        bus: Any = None,
    ) -> PatchReport:
        """Применить все патчи и вернуть отчёт.

        Args:
            config: runtime-конфиг nanobot.
            settings: ``SETTINGS`` (или его ``.gateway`` секция) — для
                ``exec_timeout_cap_sec``. Секция ``tool_result_limits``
                больше не читается ни одним патчем: её патчи сняты.
            workspace_dir: ``Path`` — корень workspace.
            agent: ``AgentLoop`` (для ``patch_assemble_outbound``).
            tool_audit_hook: ``ToolAuditHook`` (для ``patch_assemble_outbound``).
            recent_files_hook: ``RecentFilesHook`` (опционально, для
                ``patch_assemble_outbound`` — auto-attach созданных файлов
                в ``OutboundMessage.media``).
            db_logging_service: ``DbLoggingService`` (для ``patch_subagent_logging``;
                ``None`` — патч пропускается).
            session_manager: менеджер сессий (всегда класс библиотеки
                ``nanobot.session.manager.SessionManager``, у нас поверх
                ``SanitizingSessionStore``) — для персиста истории подагентов
                (может быть ``None``).

            Параметр ``cache_store`` снят в фазе 5 (п. 5.8): он был резервом
            «на будущее», ни один патч его не читал, а DI project tools
            переехал в ``lib/services/project_tool_loader.py``.

        Returns:
            ``PatchReport`` со списками ``applied`` / ``skipped`` (с причиной).
        """
        report = PatchReport()
        self._record(report, "exec_timeout_cap", self.patch_exec_timeout_cap(settings))
        self._record(report, "assemble_outbound", self.patch_assemble_outbound(
            agent, tool_audit_hook, recent_files_hook=recent_files_hook))
        self._record(report, "subagent_logging", self.patch_subagent_logging(
            db_logging_service, session_manager, bus=bus))
        self._record(report, "repeat_guard_block", self.patch_repeat_guard_block())
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
        успешный патч с частичным успехом (один из его внутренних
        шагов упал) в ``failed``.
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
          2. пишет итог запуска как ``agent.completed``;
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

            from lib.hooks.database_logging_hook import DatabaseLoggingHook, _usage_to_dict
            from lib.services.db_logging_service import LogEvent
        except Exception as exc:
            return False, f"import failed: {exc}"

        class _SubagentLoggingHook(_SubagentHook):
            """_SubagentHook + БД-логирование + персист истории подагента."""

            _sessions = session_manager
            _default_bus: Any = None
            # Когда True — ``_finalize`` пропускает прямую запись
            # ``agent.completed`` в БД, потому что
            # ``RuntimeEventsSubscriber._handle_subagent_turn_completed``
            # уже записал событие через pub-sub. Флаг управляется
            # через ``RuntimeEventsSubscriber.start()/stop()`` (см.
            # design.md D4 opencode change post-0.3.5-patches-cleanup).
            _subscriber_registered: bool = False

            @classmethod
            def set_subscriber_registered(cls, registered: bool) -> None:
                """Отметить, что ``SubagentLoggingSubscriber`` активен.

                Когда True — ``_finalize`` не пишет
                ``agent.completed`` напрямую в БД
                (handler уже записал), оставляя только
                ``finish_request`` (для question_runs) и
                ``_persist_history``.
                """
                cls._subscriber_registered = bool(registered)

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
                # Если None — публикация пропускается, agent.completed
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
                ``agent.completed`` в ``agent_gateway_logs`` через
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
                # Если подписчик активен, _finalize не пишет
                # agent.completed напрямую (handler уже записал);
                # только close_question_run. См. design.md D4.
                if getattr(
                    _SubagentLoggingHook, "_subscriber_registered", False
                ):
                    try:
                        self._db_hook._service.finish_request(
                            self._session_id,
                            status="error" if context.error else "finished",
                            summary=(
                                getattr(context, "final_content", "") or ""
                            )[:200] or None,
                            response=getattr(context, "final_content", "") or None,
                        )
                    finally:
                        self._db_hook._service.clear_request(key)
                    return
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
                        event_type="agent.completed",
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

    def patch_repeat_guard_block(self) -> tuple[bool, str]:
        """Отказ защитника от повторов → синтетический результат инструмента.

        ``nanobot.agent.tools.execution._execute_tool_call`` вызывает
        ``hook.before_execute_tool`` на строке 165 — **до** ``try``, который
        начинается строкой 166. Поэтому ``RepeatGuardBlocked`` из
        ``RepeatGuardHook`` не попадает в штатный ``except`` (строки 177-194)
        и уходит из ``execute_tool_calls`` наверх: ``asyncio.gather`` в
        параллельном режиме additionally отменяет соседние вызовы, а весь
        оборот падает с ``stop_reason="error"``.

        Hook-API не даёт «мягкого» отказа: возвращаемого значения, которое
        читает раннер, у ``AgentHook`` нет, а ``ctx.tool_calls`` — копия
        (``runner.py:483``), поэтому подменить вызов оттуда нельзя. Остаётся
        единственная точка, где результат ещё можно подменить, — сама
        ``_execute_tool_call``.

        Патч ловит **только** ``RepeatGuardBlocked`` (свой тип): любая другая
        ошибка хука остаётся видимой, а не маскируется под отказ защитника.

        Returns:
            ``(True, ...)`` при успехе; ``(False, <причина>)`` если API
            nanobot изменился (патч пропускается, режимы ``off`` и ``warn``
            продолжают работать, ``block`` деградирует до обрыва оборота —
            это громче отказа, но не тише).
        """
        try:
            from nanobot.agent.tools import execution as _exec_mod

            from lib.hooks.repeat_guard_hook import RepeatGuardBlocked
        except Exception as exc:
            return False, f"import failed: {exc}"

        original = getattr(_exec_mod, "_execute_tool_call", None)
        if original is None:
            return False, "_execute_tool_call is missing"
        if getattr(original, "_repeat_guard_patched", False):
            return True, "already patched: _execute_tool_call"

        # upstream вызывает ``_execute_tool_call`` ПОЗИЦИОННО
        # (``execution.py::execute_tool_calls``), поэтому искать ``hook`` и
        # ``context`` в ``kwargs`` бессмысленно — там их не будет никогда.
        # Биндинг по сигнатуре оригинала устойчив и к перестановке, и к
        # добавлению параметров. Считается только в ветке отказа (редко),
        # поэтому цена не имеет значения — зато она не зависит от формы
        # вызова.
        try:
            from inspect import signature as _signature

            _exec_signature = _signature(original)
        except Exception:
            _exec_signature = None

        def _resolve(args: tuple[Any, ...], kwargs: dict[str, Any], name: str) -> Any:
            if name in kwargs:
                return kwargs[name]
            if _exec_signature is None:
                return None
            try:
                return _exec_signature.bind_partial(*args, **kwargs).arguments.get(name)
            except TypeError:
                return None

        async def _execute_tool_call_guarded(*args: Any, **kwargs: Any):
            try:
                return await original(*args, **kwargs)
            except RepeatGuardBlocked as exc:
                hook = _resolve(args, kwargs, "hook")
                context = getattr(exc, "context", None)
                tool_call = getattr(exc, "tool_call", None)
                if context is None:
                    # Подстраховка: контекст лежит и в аргументах оригинала.
                    context = _resolve(args, kwargs, "context")
                if hook is not None and context is not None:
                    await hook.on_execute_tool_error(
                        context,
                        tool_call,
                        getattr(exc, "tool", None),
                        getattr(exc, "params", None),
                        exc,
                    )
                # Формат ``Error: <type>: <msg>`` — тот же, что даёт штатный
                # except в ``_execute_tool_call``, чтобы модель увидела
                # привычную ошибку инструмента, а не новый класс сообщений.
                payload = f"Error: {type(exc).__name__}: {exc}"
                try:
                    from nanobot.agent.tools.execution import _with_retry_hint

                    payload = _with_retry_hint(payload)
                except Exception:
                    # Приватный helper upstream мог исчезнуть. Без подсказки
                    # модель всё равно получит корректную ошибку — хуже
                    # восстанавливаемость, не хуже сам отказ.
                    pass
                event = {
                    "name": getattr(tool_call, "name", "?"),
                    "status": "error",
                    "detail": str(exc).replace("\n", " ").strip()[:120],
                }
                return payload, event

        _execute_tool_call_guarded._repeat_guard_patched = True  # type: ignore[attr-defined]
        try:
            _exec_mod._execute_tool_call = _execute_tool_call_guarded
        except Exception as exc:
            return False, f"patch failed: {exc}"
        return True, "tools.execution._execute_tool_call patched for RepeatGuardBlocked"

    # Вспомогательный комментарий (компакция + context-bridge seed) удалён в 0.3.5.
# Исторический audit-trail сохранён в
# openspec/changes/nanobot-035-upgrade/design.md и
# openspec/changes/runtime-events-subscription/proposal.md.

