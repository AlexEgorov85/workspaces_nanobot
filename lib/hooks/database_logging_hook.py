"""DatabaseLoggingHook — AgentHook для логирования событий агента в БД.

Реализуется как ``AgentHook`` (async-методы из nanobot.agent.hook) для
tool-событий, и использует обёртки ``publish_inbound`` /
``publish_outbound`` шины (``ApplicationContext._create_bus``) для
content-сообщений. НЕ использовать как обычный
sync-класс — он не подключается к циклу агента.

Подключение:
  * ``AgentFactory.create(..., db_logging_service=svc)`` — добавляет
    фабрику оборота ``make_db_logging_hook_factory`` в
    ``AgentLoop.from_config(hook_factories=[...])``. Фабрика создаёт
    СВЕЖИЙ ``DatabaseLoggingHook`` на каждый оборот (конкурентно-безопасно);
  * ``_create_bus(inbound_logger=..., outbound_logger=...)`` — обёртки шины.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nanobot.agent import AgentHook

if TYPE_CHECKING:
    from nanobot.agent import AgentHookContext, AgentRunHookContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Имена событий оборота — из словаря платформы
# (``mcp-platform/libs/enterprise_common/eventing/types.py``).
#
# Агент НЕ импортирует словарь: платформа объявляет этот контракт по
# протоколу, и прямой импорт сделал бы зависимость кода агента от дерева
# платформы (граница, которую репозиторий держит: агент ходит к платформе
# через MCP-операции, а не через ``sys.path``). Соответствие имён словарю
# проверяется тестом ``tests/test_turn_observability_events.py``, который
# читает словарь как файл.
#
# Почему именно эти события. До правки в журнале не было ни начала оборота,
# ни начала вызова модели, ни исхода оборота: `llm.exchanged` нёс prompt и
# response одним событием, а `agent.responded` — только текст ответа. По таблице
# нельзя было ни посчитать длительность этапа, ни ответить «успешен ли оборот».
# ---------------------------------------------------------------------------
EV_AGENT_STARTED = "agent.started"
EV_AGENT_COMPLETED = "agent.completed"
EV_AGENT_FAILED = "agent.failed"
EV_LLM_REQUESTED = "llm.requested"
EV_LLM_COMPLETED = "llm.completed"

# ---------------------------------------------------------------------------
# Мост per-iteration usage между хуком и патчами RuntimePatcher.
#
# Нужен для метрики занятости контекстного окна (``metadata.context_window``).
# Три участника:
#   * ``seed_context_window`` — патч ``agent._state_build`` (старт оборота)
#     кладёт лимит окна и модель в мост (канал сам лимит не знает);
#   * ``DatabaseLoggingHook.after_iteration`` пишет СВЕЖИЙ по-итерационный
#     ``context.usage`` (именно последняя итерация = то, что модель реально
#     видела в финальном запросе);
#   * ``RuntimePatcher.patch_assemble_outbound`` собирает готовый блок
#     ``context_window`` (usage посл. итерации ÷ лимит окна) и кладёт его
#     в metadata финального ответа;
#   * канал (postgres_channel) в фоновом цикле живого обновления читает
#     блок из моста (готовый либо собранный на лету) и пишет его в
#     processing-строку до финализации оборота.
#
# Ключ — session_key (например, ``postgres:chat_id``): разные сессии
# обрабатываются конкурентно как отдельные asyncio-задачи, без keying по
# сессии события разных чатов «перепутаются». Один проход оборота одной
# сессии выполняется последовательно (новая итерация не стартует, пока не
# окончена предыдущая), поэтому последняя запись перед ``_assemble_outbound``
# — это usage финальной итерации.
# ---------------------------------------------------------------------------
_CONTEXT_BRIDGE: dict[str, dict] = {}
_CONTEXT_BRIDGE_LOCK = threading.Lock()


def seed_context_window(
    session_key: str | None, *, limit: int = 0, model: str = ""
) -> None:
    """Засеять лимит окна/модель в мост на старте оборота.

    Вызывается из патча ``agent._state_build`` (RuntimePatcher.patch_
    context_bridge_seed): канал не знает лимит окна модели, поэтому на
    старте оборота кладём его в мост — дальше хук пишет usage каждой
    итерации, а канал собирает блок на лету (живое обновление).
    """
    if not session_key:
        return
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.setdefault(session_key, {})
        entry["limit"] = int(limit or 0)
        entry["model"] = model if isinstance(model, str) else ""


def _store_iteration_usage(session_key: str | None, usage: Any) -> None:
    """Записать по-итерационный usage оборота для сессии (неблокирующий).

    Принимает как ``dict`` (legacy), так и ``LLMUsage`` из nanobot 0.3.5+
    (через ``_usage_to_dict``). Нормализует в dict на входе, чтобы
    downstream-ридеры (``get_context_window``, ``get_iteration_usage``)
    всегда видели dict-контракт.
    """
    if not session_key:
        return
    payload = _usage_to_dict(usage)
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.setdefault(session_key, {})
        entry["usage"] = dict(payload) if payload else {}
        entry["ts"] = time.time()


def _store_context_window(session_key: str | None, block: dict | None) -> None:
    """Записать готовый блок ``context_window`` для сессии (из патча)."""
    if not session_key or not block:
        return
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.setdefault(session_key, {})
        entry["block"] = dict(block)
        entry["ts"] = time.time()


def get_context_window(session_key: str | None) -> dict | None:
    """Вернуть блок ``context_window`` сессии (без удаления).

    Предпочитаем готовый блок, собранный патчем ``_assemble_outbound``
    (точный clamp с учётом реального лимита). До финализации оборота его
    ещё нет — собираем на лету из usage последней итерации и лимита,
    засеянного на старте оборота (``seed_context_window``).
    """
    if not session_key:
        return None
    with _CONTEXT_BRIDGE_LOCK:
        entry = dict(_CONTEXT_BRIDGE.get(session_key) or {})
    block = entry.get("block")
    if isinstance(block, dict):
        return dict(block)
    usage = entry.get("usage")
    limit = int(entry.get("limit") or 0)
    if not isinstance(usage, dict) or limit <= 0:
        return None
    raw_used = usage.get("prompt_tokens")
    try:
        used = int(raw_used or 0)
    except (TypeError, ValueError):
        return None
    if used <= 0:
        return None
    return {
        "used": used,
        "limit": limit,
        "pct": round(min(1.0, used / float(limit)), 4),
        "model": entry.get("model") or "",
    }


def get_iteration_usage(session_key: str | None) -> dict | None:
    """Прочитать по-итерационный usage сессии (без удаления)."""
    if not session_key:
        return None
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.get(session_key) or {}
        usage = entry.get("usage")
        return dict(usage) if isinstance(usage, dict) else None


def pop_context_bridge(session_key: str | None) -> None:
    """Снять с моста все данные сессии (финализация/ошибка оборота)."""
    if not session_key:
        return
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop(session_key, None)


def make_db_logging_hook_factory(
    db_logging_service: Any,
    agent_id: str | None = None,
    print_llm_calls: bool = False,
    get_model: Callable[[], str | None] | None = None,
) -> Callable[[Any], DatabaseLoggingHook]:
    """Фабрика: создать СВЕЖИЙ ``DatabaseLoggingHook`` на КАЖДЫЙ оборот.

    Передаётся в ``AgentLoop`` как ``hook_factories`` (``agent_factory.py``).
    Фреймворк вызывает её с ``AgentTurnHookContext``, в котором есть
    ``session_key`` текущего оборота. Фабрика резолвит ``request_id``
    вопроса из индекса сервиса и запекает оба поля в инстанс.

    Поскольку у каждого оборота СВОЙ инстанс, состояние вопроса
    (``_run_session_key``/``_request_id``) не разделяется между
    конкурентными сессиями — события не «путаются».

    Args:
        db_logging_service: ``DbLoggingService``.
        agent_id: id агента для колонки ``agent_id`` в логах.
        print_llm_calls: печатать в терминал CLI токены каждой итерации.
        get_model: опциональный callable для резолва текущего имени
            модели в ``after_iteration``. Нужен потому что в nanobot
            0.3.5+ ``LLMResponse.model`` удалён — ``getattr(response,
            "model", None)`` всегда ``None``. ``AgentFactory``
            замыкает ``get_model`` над ``lambda: agent.model``.
            Если не передан — ``DatabaseLoggingHook.model`` остаётся
            ``None``, и ``log_llm_call`` пишет ``name="llm"``
            (как и до фикса).

    Returns:
        Фабрика ``def(turn_context) -> DatabaseLoggingHook``.
    """

    def _factory(turn_context: Any) -> DatabaseLoggingHook:
        session_key = getattr(turn_context, "session_key", None) or None
        request_id = None
        # ``request_id`` оборота берётся из индекса сервиса, и он МОЖЕТ БЫТЬ
        # ПУСТЫМ: у входящего не было ``message_id`` взятой строки очереди
        # (фоновый вызов, правка в ``cli``). Выдумывать его здесь нельзя —
        # второй источник подстановки, который подписал 99,3 % строк журнала
        # чужим именем (change 2026-10-04-queue-as-anchor-identity, Ф0.1).
        # Пустое значение не лишает события личности: ``user_id`` подставляет
        # сервис из снимка личности ВХОДА той же сессии, а строку прогона
        # никто и не должен был создавать — вопроса не было.
        if session_key and db_logging_service is not None:
            request_id = db_logging_service.get_request_id(session_key)
        return DatabaseLoggingHook(
            db_logging_service,
            agent_id=agent_id,
            session_key=session_key,
            request_id=request_id,
            print_llm_calls=print_llm_calls,
            get_model=get_model,
        )

    return _factory


def _current_request_sender_id() -> str | None:
    """``RequestContext.sender_id`` текущего request (или ``None``).

    Единственная точка обращения к identity-store в этом модуле.
    Инкапсулирует зависимость от nanobot 0.3.0: если поле будет переименовано,
    адаптация делается в этой функции.

    **Из фабрики хуков она больше не зовётся, и это не потеря.** Фабрика
    больше не регистрирует оборот (выдуманный ``request_id`` запрещён), а
    личность событий подставляет сервис — из снимка личности ВХОДА, который
    кладёт ``register_request`` на входящем сообщении, где ``sender_id`` и
    так известен. Функция остаётся, потому что это единственное место, где
    известно устройство identity-store nanobot, и её подменяют в тестах
    (``tests/test_subagent_logging.py``).
    """
    # Чтение личности оборота живёт в lib/services/turn_identity.py:
    # тот же contextvar читались хук подписи вызовов, клиент платформы,
    # подписчик событий и ещё три места, и копии правила разъезжались бы
    # молча — подпись в журнале и файл сессии описали бы разные вызовы.
    # Импорт ленивый: функцию подменяют в тестах по имени, и лишний
    # импорт на этапе сборки модуля ей не нужен.
    from lib.services.turn_identity import read_turn_context

    turn = read_turn_context()
    return turn.user_id if turn is not None else None


def _messages_chars(messages: Any) -> int:
    """Суммарный размер текста промпта в символах (дешёвый размер запроса).

    Нужен ``llm.requested``: сам промпт там не дублируется (он лежит в
    ``llm.exchanged``), но без размера начало вызова модели не с чем связать, кроме
    номера итерации. Считается одним проходом по уже существующим строкам —
    ни копий, ни сериализации.
    """
    total = 0
    for message in messages or ():
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, (list, tuple)):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(part["text"])
    return total


def _usage_to_dict(usage: Any) -> dict | None:
    """Унифицированный адаптер ``LLMUsage | dict | None -> dict | None``.

    Возвращает ``dict`` с per-turn полями (``prompt_tokens``,
    ``completion_tokens``, ``total_tokens``, ...) для записи в БД и
    payload-события. Принимает как dataclass ``nanobot.llm_usage.models.LLMUsage``
    (с методом ``to_turn_dict()``), так и legacy-``dict``. На любом
    неожиданном типе или сбое конверсии — ``None`` (fail-soft).
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage) if usage else None
    to_turn = getattr(usage, "to_turn_dict", None)
    if callable(to_turn):
        try:
            payload = to_turn()
        except Exception:
            return None
        return dict(payload) if isinstance(payload, dict) and payload else None
    to_dict = getattr(usage, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:
            return None
        return dict(payload) if isinstance(payload, dict) and payload else None
    return None


class DatabaseLoggingHook(AgentHook):
    """Агентский хук — пересылает tool- и run-события в DbLoggingService.

    Живёт в ``lib/hooks/``: это фреймворковый хук, а не плагин
    ``workspace/hooks/``. Он требует обязательный ``db_logging_service``
    в конструкторе, который ``hook_loader`` предоставить не может,
    поэтому в auto-scan ``workspace/hooks/`` не участвует (он и не
    сканируется — плагин-директория содержит только самодостаточные
    хуки с контрактом ``cls(workspace_dir=...)``).

    Создаётся per-turn через ``make_db_logging_hook_factory`` в
    ``AgentFactory`` или явно в ``RuntimePatcher.patch_subagent_logging``.

    Все методы НЕБЛОКИРУЮЩИЕ: ``DbLoggingService.log_*`` ставит события
    в очередь и возвращает ``True/False`` мгновенно.

    Конкурентность: фреймворковый ``AgentRunHookContext`` (для ``after_run``)
    не содержит ``session_key``, поэтому контекст вопроса раньше кэшировался
    в полях инстанса (``_run_session_key``/``_request_id``). При конкурентной
    обработке нескольких сессий одним общим инстансом поля перезаписывались
    чужим вопросом — события «путались».

    Теперь инстанс создаётся НА КАЖДЫЙ оборот через ``make_db_logging_hook_factory``
    и запекает свой ``session_key``/``request_id`` в конструкторе: состояние
    вопроса изолировано между сессиями, гонки нет.
    """

    def __init__(
        self,
        db_logging_service: Any,
        agent_id: str | None = None,
        *,
        session_key: str | None = None,
        request_id: str | None = None,
        print_llm_calls: bool = False,
        get_model: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._service = db_logging_service
        self._tool_start_times: dict[str, float] = {}
        self._agent_id = agent_id
        self._print_llm_calls = print_llm_calls
        # Closure для резолва текущей модели. Закрывается фабрикой
        # (см. ``make_db_logging_hook_factory``). Вызывается в
        # ``after_iteration`` — в nanobot 0.3.5+ ``LLMResponse.model``
        # удалён, поэтому читать надо с ``agent.model`` (property
        # ``nanobot/agent/loop.py:218`` → ``runtime_resolver.runtime.model``).
        self._get_model: Callable[[], str | None] | None = get_model
        # Контекст текущего оборота/вопроса. Запекается в фабрике на оборот,
        # чтобы ``after_run`` (у которого в контексте нет session_key) знал
        # свой вопрос. ``_capture_context`` дополнительно перечитывает
        # request_id по session_key из индекса сервиса — самокорректно.
        self._run_session_key = session_key
        self._request_id = request_id
        # Снимок промпта текущей итерации (полный messages), чтобы
        # ``after_iteration`` мог упаковать его вместе с ответом в llm.exchanged.
        self._pending_prompt: list | None = None
        self._pending_iteration: int | None = None
        # Момент начала оборота (``before_run``) — из него считается
        # длительность оборота, которую раньше приходилось выводить вручную
        # по десяткам строк.
        self._turn_started_at: float | None = None
        # Терминальное событие оборота пишется ровно одно: nanobot зовёт
        # ``on_error`` и ``after_run`` последовательно при ошибке без
        # исключения, а ``after_run`` — и вовсе без ``on_error``. Флаг
        # гарантирует один ``agent.completed`` ИЛИ один ``agent.failed``,
        # а не оба и не ноль.
        self._turn_terminal_logged = False
        # Момент начала текущей итерации LLM — длительность вызова модели.
        self._iteration_started_at: float | None = None

    # ------------------------------------------------------------------
    # Запись событий оборота
    # ------------------------------------------------------------------

    def _log_stage(
        self,
        event_type: str,
        *,
        name: str,
        summary: str,
        payload: dict | None = None,
        metadata: dict | None = None,
        level: str = "INFO",
    ) -> None:
        """Записать событие оборота от имени хука.

        Fail-soft: падение записи не должно ронять ход агента (та же политика,
        что у ``log_tool_call`` и ``agent.responded``), но и не теряется молча —
        уходит в WARNING хука. Момент СОБЫТИЯ и ключ порядка проставляет
        ``DbLoggingService.log_event`` (единственная точка входа), поэтому
        здесь ничего про время писать не нужно.
        """
        from lib.services.db_logging_service import LogEvent

        try:
            self._service.log_event(LogEvent(
                event_type=event_type,
                level=level,
                session_id=self._run_session_key,
                request_id=self._request_id,
                actor="agent",
                name=name,
                summary=summary,
                payload=payload or {},
                metadata=metadata or {},
            ))
        except Exception as exc:  # noqa: BLE001 - оборота не роняем
            logger.warning(
                "DbLoggingHook: событие %s не записано: %s", event_type, exc
            )

    def _resolve_model(self) -> str | None:
        """Имя текущей модели (или ``None``).

        В nanobot 0.3.5+ ``LLMResponse.model`` удалён — модель живёт на
        ``AgentLoop.model`` (свойство
        ``nanobot/agent/loop.py:218`` → ``runtime_resolver.runtime.model``),
        поэтому хук берёт её через замыкание ``get_model``, заданное
        ``make_db_logging_hook_factory``. Вынесено в одну точку, потому что
        ``llm.requested``, ``llm.completed`` и ``llm.exchanged`` обязаны назвать
        ОДНУ и ту же модель: разъезд имён в трёх событиях одной итерации
        сделал бы разбор вызова модели невозможным.
        """
        if not self._get_model:
            return None
        try:
            return self._get_model()
        except Exception:  # noqa: BLE001 - имя модели не критично
            return None

    def _turn_latency_ms(self) -> float | None:
        """Длительность оборота от ``before_run`` (или ``None`` без старта)."""
        if self._turn_started_at is None:
            return None
        return round((time.time() - self._turn_started_at) * 1000.0, 3)

    # ------------------------------------------------------------------
    # Tool-события
    # ------------------------------------------------------------------

    def _capture_context(self, context: Any) -> None:
        """Подхватить session_key и request_id текущего вопроса."""
        session_key = getattr(context, "session_key", None)
        if not session_key:
            return
        self._run_session_key = session_key
        self._request_id = self._service.get_request_id(session_key)

    def _ctx(self, key: str) -> str | None:
        # Упрощено: контекст вопроса теперь живёт в question_runs,
        # в события gateway_logs идёт только request_id.
        if key == "request_id":
            return self._request_id
        if key == "agent_id":
            return self._agent_id
        return None

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        tool_call_id = str(getattr(tool_call, "id", None) or id(tool_call))
        self._tool_start_times[tool_call_id] = time.time()
        self._capture_context(context)
        try:
            self._service.log_tool_call(
                session_id=context.session_key or "",
                tool_name=str(getattr(tool_call, "name", "?")),
                args=params if isinstance(params, dict) else {},
                tool_call_id=tool_call_id,
                request_id=self._request_id,
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.before_execute_tool failed: %s", exc)

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        tool_call_id = str(getattr(tool_call, "id", None) or id(tool_call))
        start = self._tool_start_times.pop(tool_call_id, None)
        latency_ms = (time.time() - start) * 1000.0 if start is not None else 0.0
        try:
            self._service.log_tool_result(
                session_id=context.session_key or "",
                tool_name=str(getattr(tool_call, "name", "?")),
                result=result,
                latency_ms=latency_ms,
                tool_call_id=tool_call_id,
                status="ok",
                request_id=self._request_id,
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.after_execute_tool failed: %s", exc)

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        """Отказ инструмента в журнал пишет ``ToolAuditHook``, а не этот хук.

        Оба видят один и тот же отказ: и сюда, и в ``after_iteration`` хука
        аудита он приходит одним и тем же вызовом. Писать отсюда и оттуда
        означало бы две строки ``tool.failed`` на один отказ, а журнал читают
        как одну запись на событие.

        Владелец выбран не случайно: ``ToolAuditHook`` есть в ``ctx.hooks``
        безусловно, а этот хук — только при ``DbLoggingService`` и только
        на время оборота. Отказ, случившийся вне оборота, обязан быть записан
        стороной, которая переживает оборот.
        """
        # Время старта всё же снимаем: иначе запись для этого вызова останется
        # в ``_tool_start_times`` навсегда и утечёт по одной записи на отказ.
        tool_call_id = str(getattr(tool_call, "id", None) or id(tool_call))
        self._tool_start_times.pop(tool_call_id, None)

    # ------------------------------------------------------------------
    # Run-level summary
    # ------------------------------------------------------------------

    async def before_run(self, context: AgentRunHookContext) -> None:
        """Оборот взят в обработку — ПЕРВОЕ событие оборота в журнале.

        До него в журнале есть только ``agent.received``: сообщение опубликовано
        в шину и ждёт. Разница между моментами ``agent.received`` и
        ``agent.started`` —
        это время ожидания в очереди плюс restore/compact сессии, и без
        ``agent.started`` его было неоткуда взять: ни в коде, ни в таблице
        события начала оборота не существовало.

        Выбран именно этот хук, потому что он ПУБЛИЧНАЯ точка nanobot и
        вызывается на каждый ``runner.run()`` — раньше от ``agent.received``
        (шина) и раньше ``DatabaseLoggingHook.after_run``. Альтернативы
        потребовали бы
        патча ``AgentLoop`` (запрещено политикой инвентаря) или второго
        хука ради одного события.

        ``AgentRunHookContext`` не содержит ``session_key``, поэтому личность
        оборота берётся из полей инстанса, запечённых фабрикой на оборот.
        """
        self._turn_started_at = time.time()
        self._turn_terminal_logged = False
        self._log_stage(
            EV_AGENT_STARTED,
            name="turn",
            summary="оборот взят в обработку",
        )

    def _log_turn_terminal(self, context: AgentRunHookContext, *, failed: bool) -> None:
        """Терминальное событие оборота — ровно одно (``agent.completed``/``agent.failed``).

        Исход берётся из ``context.error``, а длительность — из ``before_run``,
        поэтому на вопрос «был ли оборот успешным и сколько занял» отвечает
        сам журнал, а не ручной разбор строк. Значение ``stop_reason`` и
        usage пишутся в metadata, чтобы исход читался без payload'а.

        Текст ответа сюда НЕ кладётся: по спецификации журнала
        (``openspec/specs/observability/logging-db/spec.md``, «agent.responded сохраняет
        текст финального ответа») формирование ответа, исход оборота и
        доставка — три разных факта и три разных строки. Текст несёт
        ``agent.responded`` (ранее ``run_finished``); смешивать их
        нельзя, иначе исход оборота стал бы зависеть от наличия ответа.
        """
        if self._turn_terminal_logged:
            return
        self._turn_terminal_logged = True
        usage_dict = _usage_to_dict(getattr(context, "usage", None)) or {}
        metadata: dict[str, Any] = {
            "outcome": "failed" if failed else "completed",
            "latency_ms": self._turn_latency_ms(),
            "stop_reason": getattr(context, "stop_reason", None),
            "iterations": self._pending_iteration,
            "tokens_used": usage_dict.get("total_tokens"),
        }
        if failed:
            self._log_stage(
                EV_AGENT_FAILED,
                level="ERROR",
                name="turn",
                summary=(getattr(context, "error", None) or "оборот прерван")[:200],
                payload={"error": getattr(context, "error", None)},
                metadata=metadata,
            )
        else:
            self._log_stage(
                EV_AGENT_COMPLETED,
                name="turn",
                summary="оборот завершён",
                metadata=metadata,
            )

    async def on_error(self, context: AgentRunHookContext) -> None:
        """Оборот упал — фиксируем исход, пока не потеряли контекст.

        nanobot зовёт ``on_error`` и при исключении (с последующим re-raise, без
        ``after_run``), и при ошибке, пережившей обёртку итерации (тогда
        ``after_run`` будет вызван следом). Флаг в ``_log_turn_terminal``
        оставляет ровно одно терминальное событие в обоих случаях.
        """
        self._log_turn_terminal(context, failed=True)

    async def before_iteration(self, context: Any) -> None:
        # AgentRunHookContext не содержит session_key, ловим его здесь
        # (передаётся AgentHookContext со spec.session_key)
        self._capture_context(context)
        # Полный промпт (messages) этой итерации — снимок до ответа,
        # чтобы ``after_iteration`` упаковал его вместе с LLMResponse.
        self._pending_prompt = list(getattr(context, "messages", None) or [])
        self._pending_iteration = getattr(context, "iteration", None)
        self._iteration_started_at = time.time()
        # Начало вызова модели. Раньше модель была видна только по итогу:
        # ``llm.exchanged`` нёс prompt и response ОДНИМ событием, записанным
        # после ответа, поэтому длительность вызова и сам факт «пошёл запрос» не
        # оставляли в журнале никакого следа. Здесь пишется только размер
        # запроса (полный промпт остаётся в ``llm.exchanged``) — чтобы связать
        # начало с архивом по ``iteration``, не дублируя мегабайты текста.
        messages = self._pending_prompt
        self._log_stage(
            EV_LLM_REQUESTED,
            name="llm",
            summary=f"запрос итерации {self._pending_iteration} к модели",
            payload={
                "iteration": self._pending_iteration,
                "messages": len(messages),
                "prompt_chars": _messages_chars(messages),
            },
            metadata={
                "iteration": self._pending_iteration,
                "model": self._resolve_model(),
            },
        )

    async def after_iteration(self, context: Any) -> None:
        self._capture_context(context)
        # Свежий по-итерационный usage — мост для метрики занятости окна
        # (последняя запись перед финалом = usage финальной итерации).
        _store_iteration_usage(self._run_session_key, getattr(context, "usage", None))
        response = getattr(context, "response", None)
        # Конец вызова модели — ПЕРЕД ранним выходом по ``response is None``:
        # итерация, не получившая ответа, тоже была вызовом модели, и её
        # длительность иначе потерялась бы. Пишется до ``llm.exchanged``, чтобы
        # в порядке событий конец запроса не обгонял его архив.
        iteration = self._pending_iteration or getattr(context, "iteration", None)
        latency_ms = (
            round((time.time() - self._iteration_started_at) * 1000.0, 3)
            if self._iteration_started_at is not None
            else None
        )
        # Модель резолвится ОДИН раз на итерацию: ``llm.completed`` и
        # ``llm.exchanged`` обязаны назвать одну и ту же, а второй вызов
        # ``agent.model`` на итерацию — лишняя работа в горячем пути и
        # лишний вызов ``get_model``, который contract-тест считает.
        model = self._resolve_model()
        self._log_stage(
            EV_LLM_COMPLETED,
            level="ERROR" if getattr(context, "error", None) else "INFO",
            name="llm",
            summary=f"ответ итерации {iteration} получен",
            payload={"iteration": iteration},
            metadata={
                "iteration": iteration,
                "model": model,
                "latency_ms": latency_ms,
                "finish_reason": getattr(response, "finish_reason", None),
                "usage": _usage_to_dict(getattr(context, "usage", None)) or {},
            },
        )
        if response is None:
            return
        try:
            from dataclasses import asdict

            # Резолв имени модели: в nanobot 0.3.5+ ``LLMResponse.model``
            # удалён — он теперь живёт на ``AgentLoop.model`` (свойство
            # ``runtime_resolver.runtime.model``). Фабрика
            # ``make_db_logging_hook_factory`` принимает опциональный
            # ``get_model`` callable, закрывающийся над ``agent.model``
            # (см. ``AgentFactory.create``). Если callable не передан
            # (тесты, fallback) — пишем с ``model=None``, и
            # ``log_llm_call`` ставит ``name="llm"``.
            # ``level="DEBUG"`` — канонический уровень события-носителя тел
            # обмена с моделью, решение заказчика от 2026-10-03: «Вызов LLM
            # нужно логировать, но с типом DEBUG». Обмен — самый крупный
            # носитель в журнале (~62 КБ на строку, из них ~59 КБ — дословная
            # копия аргументов tool-вызовов), и на ``INFO`` он не о событии
            # оборота, а о внутренностях промпта.
            #
            # Уровень задан ЯВНО здесь, а не оставлен дефолтом
            # ``log_llm_call``: дефолт однажды сменили бы обратно, и размен
            # стал бы незаметным. Канон закреплён стражем
            # ``tests/test_journal_level_canonical.py::test_llm_exchange_is_published_as_debug``
            # — без списка исключений.
            #
            # ВНИМАНИЕ, последствие размена (проверено, не предположено):
            # при боевом ``logging.db.min_level = "INFO"`` фильтр агента
            # (``DbLoggingService._should_log``) отбрасывает ``DEBUG`` ДО
            # постановки в очередь, то есть строка в таблицу не попадает
            # вовсе. Пока порог не опущен до ``DEBUG``, «логировать» означает
            # «писать, когда порог разрешит», а не «писать всегда». См.
            # открытый вопрос в отчёте по change.
            self._service.log_llm_call(
                session_id=self._run_session_key or "",
                prompt=self._pending_prompt or [],
                response=asdict(response),
                iteration=iteration,
                model=model,
                finish_reason=getattr(response, "finish_reason", None),
                usage=_usage_to_dict(getattr(context, "usage", None)) or {},
                request_id=self._request_id,
                level="DEBUG",
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.after_iteration llm_call failed: %s", exc)
        if self._print_llm_calls:
            self._print_llm_tokens(context)

    def _print_llm_tokens(self, context: Any) -> None:
        """Вывести в терминал две строки о токенах итерации (CLI-режим).

        Раньше использовался ``Rich Console.print("[dim]...")``, который на
        legacy Windows-консоли (cmd/PowerShell ISE без VT) рендерил
        ``?[2m→ LLM: ...?[0m`` из-за подмены ESC на ``?``. Эти строки —
        debug-вывод; декоративный dim-стиль тут не нужен, важен сам факт
        вывода. Используем ``print()`` напрямую — никакого ANSI.
        """
        usage = _usage_to_dict(getattr(context, "usage", None)) or {}
        if not usage:
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return
        if prompt is not None:
            print(f"→ LLM: отправлен промпт ({prompt} токенов)")
        if completion is not None:
            print(f"← LLM: получен ответ ({completion} токенов)")

    async def after_run(self, context: AgentRunHookContext) -> None:
        try:
            self._service.log_event(
                _make_run_event(context, self._run_session_key, self._request_id)
            )
            if self._request_id:
                self._service.finish_request(
                    self._request_id,
                    status="error" if context.error else "finished",
                    summary=(context.final_content or "")[:200] or None,
                    response=context.final_content or None,
                )
            # Исход оборота — после ``agent.responded`` (который несёт текст
            # ответа) и ДО ``finally``: снимок личности вопроса живёт до конца
            # ``try``. При ошибке, пережившей обёртку итерации, ``on_error``
            # мог уже записать ``agent.failed`` — повторно не пишется.
            self._log_turn_terminal(context, failed=bool(context.error))
        except Exception as exc:
            logger.warning("DbLoggingHook.after_run failed: %s", exc)
        finally:
            if self._run_session_key:
                self._service.clear_request(self._run_session_key)
                self._request_id = None


def _make_run_event(
    context: AgentRunHookContext,
    session_key: str | None = None,
    request_id: str | None = None,
):
    """Сформировать LogEvent из AgentRunHookContext (без жёсткой связки)."""
    from lib.services.db_logging_service import LogEvent

    final = context.final_content or ""
    tools = context.tools_used or []
    usage_dict = _usage_to_dict(getattr(context, "usage", None)) or {}
    payload: dict[str, Any] = {
        "final_content": final,
        "tools_used": tools,
        "stop_reason": context.stop_reason,
        "had_injections": context.had_injections,
    }
    if request_id:
        payload["request_id"] = request_id
    return LogEvent(
        event_type="agent.responded",
        level="ERROR" if context.error else "INFO",
        actor="agent",
        name="run",
        session_id=session_key,
        request_id=request_id,
        summary=final[:200],
        payload=payload,
        metadata={
            "tokens_used": usage_dict.get("total_tokens"),
            "had_error": bool(context.error),
        },
    )
