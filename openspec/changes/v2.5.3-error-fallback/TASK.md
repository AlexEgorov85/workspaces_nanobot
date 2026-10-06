# TASK: error-fallback на `release/v2.5.3`

## Контекст

- **Цель release**: `release/v2.5.3` (HEAD тега `v2.5.3` = `0c474a38`), `nanobot-ai==0.3.0`.
- **Источник правды** (где фича уже есть и протестирована): ветка `backup/master-pre-future-work-2026-10-02` (== master HEAD `32d61a7c`, `nanobot-ai==0.3.5`).
- **Совместимость с 0.3.0 проверена эмпирически** (поставлен `nanobot-ai==0.3.0` в чистый venv, проверены `TurnDelivery.fail` сигнатура и поведение). API совместим — **апгрейд nanobot НЕ требуется**.
- **Известное отличие 0.3.0 от 0.3.5**: в 0.3.0 на `TurnDelivery` нет атрибута `_failure_error_kind`, и `runtime_event_publisher.turn_completed(...)` НЕ принимает kwargs `outcome`/`failure_kind`. Патч это **уже** обрабатывает (через `getattr(self, "_failure_error_kind", None)` — возврат `None`); один тест нужно ослабить (см. Шаг 7.4).
- **Все `file:line` ниже сверены с `git show release/v2.5.3:<path>`** (== `git show v2.5.3:<path>`).

## Шаг 0. Проверка предпосылки (апгрейд НЕ нужен)

```bash
python -c "from nanobot.agent.turn_delivery import TurnDelivery; import inspect; print(inspect.signature(TurnDelivery.fail))"
```

Ожидаемый вывод: `<Signature (self, *, publish_completion: bool) -> None>`. Это и есть подтверждение, что API совместим с патчем. Если `AttributeError` — что-то не так с установкой nanobot, дальше не идти.

> **Апгрейд `nanobot-ai` 0.3.0 → 0.3.5 НЕ выполнять.** Ранние версии этого документа требовали обязательный upgrade — это было ошибкой. Поведение `TurnDelivery.fail` в обеих версиях идентично (одинаковый литерал «Sorry, I encountered an error.», одинаковый `publish_outbound`, одинаковый вызов `turn_completed` без `outcome`/`failure_kind`). Различия только в kwargs `turn_completed` и наличии `_failure_error_kind` — патч это уже учитывает.

---

## Шаг 1. Файл `lib/services/runtime_patcher.py` — добавить патч

Файл: 2217 строк. Все правки **в конец** секции с хелперами и **перед** `class _OutboundSilencer`/патч-методами. Точные места — по контексту.

### 1.1. Импорт `sys as _sys` (шапка, строка 31)

**Найти** строку 31:
```python
from lib.utils.node_access import get_path as _get
```

**Добавить сразу после** (новая строка 32):
```python
import sys as _sys
```

> **Проверка**: в v2.5.3 `_sys` уже импортируется как `import sys as _sys` на строке 18. **Повторно не добавлять**, шаг пропустить.

### 1.2. Константы (после `def _session_key_of` ~строка 46, перед `def _resolve_media_path` ~строка 58)

**Вставить блок** (новая позиция):
```python
# ---------------------------------------------------------------------------
# Default для ``gateway.error_messages`` (RuntimePatcher.patch_turn_delivery_fail)
# ---------------------------------------------------------------------------
_DEFAULT_INTERNAL_ERROR_TEXT: str = (
    "Я не справился с вашим вопросом. "
    "Попробуйте, пожалуйста, переформулировать конкретнее — "
    "например, уточните ключевую часть или приведите пример."
)
_DEFAULT_LOG_TO_DB: bool = True
```

### 1.3. Класс `_OutboundSilencer` (после констант)

**Вставить блок**:
```python
class _OutboundSilencer:
    """Прокси для подавления outbound'а при вызове upstream ``TurnDelivery.fail``.

    Используется в ``RuntimePatcher.patch_turn_delivery_fail._wrap_fail``:
    на время вызова оригинального ``fail()`` ``self.bus`` подменяется на
    этот объект. ``__getattr__`` пробрасывает все обращения к реальному
    bus, кроме ``publish_outbound`` — она возвращает ``None`` без публикации.
    Это позволяет сохранить upstream-логику ``turn_completed`` runtime-event,
    не отправляя при этом upstream-литерал ``"Sorry, I encountered an error."``
    пользователю.

    Подмена атрибута экземпляра (per-instance) безопасна для конкурентных
    оборотов: один ``TurnDelivery`` живёт ровно один оборот.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def publish_outbound(self, msg: Any) -> None:
        return None
```

### 1.4. Хелпер `_resolve_agent_id` (после `_OutboundSilencer`)

**Вставить блок**:
```python
def _resolve_agent_id(config: Any, agent: Any) -> str | None:
    """Резолв идентификатора активного агента для передачи в патчи.

    Источники по убыванию приоритета:
    1. ``config.agents.defaults.name`` (если задано явно).
    2. ``config.default_agent`` (если есть).
    3. ``agent.name`` (fallback на переданный ``AgentLoop``).
    4. ``None`` если ничего не удалось достать.
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
```

### 1.5. PatchSpec в `_PATCH_SPECS` (строки 158-…)

**Найти** закрывающую скобку `_PATCH_SPECS: dict[str, PatchSpec] = {` (строка 158). Внутри — 15 записей: `context_governor`, `save_turn`, `exec_limits`, `exec_timeout_cap`, `tool_limits`, `assemble_outbound`, `context_bridge_seed`, `async_save`, `session_dir_watch`, `subagent_logging`, `project_tools`, `compact_tracking`, `compact_command`, `idle_guard`, `session_content_cleanup`, `document_text_threshold`.

> **После шага 0** запись `context_bridge_seed` удалена → осталось **14 записей**. Новую `turn_delivery_fail` добавить **последней** (перед `}`).

**Вставить запись**:
```python
"turn_delivery_fail": PatchSpec(
    name="turn_delivery_fail",
    purpose="конфигурируемый fallback-ответ при internal-ошибке в "
            "AgentLoop._process_message (подмена захардкоженного "
            "upstream-литерала \"Sorry, I encountered an error.\")",
    nanobot_target="nanobot.agent.turn_delivery.TurnDelivery.fail",
    reason="public extension point отсутствует; патч подменяет метод "
           "класса обёрткой, читает gateway.error_messages.internal_error, "
           "подавляет двойной outbound через per-instance _OutboundSilencer "
           "и пишет event_type=\"turn_failed\" в agent_gateway_logs",
    alternatives_checked="AgentLoop(turn_delivery_factory=...) не существует "
                         "в nanobot 0.3.0/0.3.5; единственная точка расширения — "
                         "monkey-patch метода класса",
    risk="medium",
    nanobot_version="0.3.0",
),
```

> **Поле `nanobot_version`** — в v2.5.3 `PatchSpec` имеет поле `nanobot_version` (см. `TestPatchSpecs.test_specs_have_required_fields` строка 1151-1162). Заполнить обязательно, иначе тест упадёт. Ставим `0.3.0` — фича проверена на этой версии.

### 1.6. Метод `patch_turn_delivery_fail` (внутри `class RuntimePatcher`, **после** `patch_session_content_cleanup`, **перед** `return report`)

**Найти** последний `_record(...)` в `apply_all` (строка 496 — `session_content_cleanup`) и **после него** вставить (внутри тела `apply_all`, перед `return report` строка 497):
```python
        self._record(report, "turn_delivery_fail", self.patch_turn_delivery_fail(
            settings, db_logging_service, agent_id=_resolve_agent_id(config, agent)))
```

**Затем в теле `class RuntimePatcher`**, после метода `patch_session_content_cleanup` (строка ~963, после `return True, "..."`) вставить целиком новый метод:

```python
    def patch_turn_delivery_fail(
        self,
        settings: Any,
        db_logging_service: Any = None,
        agent_id: str | None = None,
    ) -> tuple[bool, str]:
        """Заменить ``TurnDelivery.fail`` обёрткой с заготовленным текстом.

        Upstream-``nanobot.agent.turn_delivery.TurnDelivery.fail``
        (``site-packages/.../turn_delivery.py:336-353``) при любом
        ``Exception`` в ``AgentLoop._process_message`` отправляет
        пользователю хардкод ``"Sorry, I encountered an error."``.
        Патч подменяет метод класса обёрткой, которая:

        1. читает ``gateway.error_messages.internal_error`` из SETTINGS
           (default — ``_DEFAULT_INTERNAL_ERROR_TEXT``);
        2. формирует ``OutboundMessage`` с ``content=internal_error``,
           ``metadata._error_kind="internal"`` и оригинальным
           ``channel/chat_id/metadata`` из ``self.lifecycle_message``;
        3. при ``log_to_db=True`` (default) и доступном
           ``db_logging_service`` пишет в ``agent_gateway_logs`` через
           ``try_log_event`` (``event_type="turn_failed"``, payload c типом
           и текстом исключения) — без утечки деталей пользователю;
        4. вызывает оригинальный ``TurnDelivery.fail(self, publish_completion=...)``
           для финализации ``turn_completed`` event (run-time event publisher).

        ``asyncio.CancelledError`` НЕ проходит через ``fail()`` — в
        upstream он обрабатывается отдельной веткой ``except`` в
        ``_process_message`` и зовёт ``delivery.abort_stream()``. Патч
        НЕ вмешивается в эту ветку (перехват именно на ``fail``).

        Args:
            settings: ``SETTINGS`` (или ``AttrDict``-проекция ``.gateway.*``).
                ``None`` → default-текст, ``log_to_db=True``.
            db_logging_service: ``DbLoggingService`` или ``None``. При
                ``None`` — запись в БД пропускается (fail-open).
            agent_id: идентификатор агента для колонки ``agent_id`` в
                ``agent_gateway_logs`` payload (опционально).

        Returns:
            ``(True, "TurnDelivery.fail patched")`` при успехе;
            ``(False, <причина>)`` если ``TurnDelivery`` модуль не
            загружен (битый nanobot / нет в ``sys.modules``).
        """
        td_module = _getloaded("nanobot.agent.turn_delivery")
        if td_module is None:
            return False, "TurnDelivery module not loaded"

        internal_error = _get(
            settings, "gateway", "error_messages", "internal_error",
            default=None,
        )
        if not isinstance(internal_error, str) or not internal_error:
            internal_error = _DEFAULT_INTERNAL_ERROR_TEXT

        log_to_db = _get(
            settings, "gateway", "error_messages", "log_to_db", default=None,
        )
        if not isinstance(log_to_db, bool):
            log_to_db = _DEFAULT_LOG_TO_DB

        try:
            TurnDelivery = getattr(td_module, "TurnDelivery")
        except AttributeError:
            return False, "TurnDelivery class not found in module"
        original_fail = getattr(TurnDelivery, "fail", None)
        if original_fail is None:
            return False, "TurnDelivery.fail is missing"

        async def _wrap_fail(self, *, publish_completion: bool) -> None:
            from lib.services.db_logging_service import LogEvent, try_log_event

            exc = _sys.exception()

            lifecycle = getattr(self, "lifecycle_message", None)
            channel = getattr(lifecycle, "channel", None) if lifecycle else None
            chat_id = getattr(lifecycle, "chat_id", None) if lifecycle else None
            base_metadata = (
                dict(getattr(lifecycle, "metadata", None) or {})
                if lifecycle is not None
                else {}
            )

            session_key = getattr(self, "session_key", None)
            sender_id = (
                getattr(lifecycle, "sender_id", None)
                if lifecycle is not None
                else None
            )

            failure_error_kind = getattr(self, "_failure_error_kind", None)

            exception_available = exc is not None
            exception_type = (
                type(exc).__name__ if exc is not None else None
            )
            exception_message = str(exc) if exc is not None else None

            outbound_metadata = dict(base_metadata)
            outbound_metadata["_error_kind"] = "internal"
            outbound_metadata["_final_turn"] = True

            try:
                from nanobot.bus.events import OutboundMessage
            except Exception:
                OutboundMessage = None  # type: ignore[assignment]

            if OutboundMessage is not None:
                try:
                    outbound = OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=internal_error,
                        metadata=outbound_metadata,
                    )
                    bus = getattr(self, "bus", None)
                    publish_outbound = getattr(bus, "publish_outbound", None)
                    if callable(publish_outbound):
                        result = publish_outbound(outbound)
                        if asyncio.iscoroutine(result):
                            await result
                except Exception as exc_pub:
                    logger.warning(
                        "TurnDelivery.fail wrapper: failed to publish "
                        "fallback outbound: {}",
                        exc_pub,
                    )

            if log_to_db and db_logging_service is not None:
                try:
                    summary_text = (
                        str(failure_error_kind)
                        if failure_error_kind
                        else "turn_failed"
                    )
                    log_event = LogEvent(
                        event_type="turn_failed",
                        level="ERROR",
                        session_id=(
                            session_key
                            if isinstance(session_key, str)
                            else None
                        ),
                        channel=channel,
                        actor=None,
                        summary=summary_text,
                        payload={
                            "kind": "internal",
                            "failure_error_kind": failure_error_kind,
                            "agent_id": agent_id,
                            "sender_id": sender_id,
                            "chat_id": chat_id,
                            "exception_type": exception_type,
                            "exception_message": exception_message,
                            "exception_available": exception_available,
                        },
                        metadata={
                            "fallback_text_len": len(internal_error),
                            "publish_completion": bool(publish_completion),
                        },
                        user_id=(
                            sender_id
                            if isinstance(sender_id, str)
                            else None
                        ),
                    )
                    try_log_event(
                        db_logging_service,
                        log_event,
                        producer="runtime_patcher",
                        event_type="turn_failed",
                    )
                except Exception as exc_log:
                    logger.warning(
                        "TurnDelivery.fail wrapper: failed to build "
                        "log_event: {}",
                        exc_log,
                    )

            original_bus = getattr(self, "bus", None)
            if original_bus is not None:
                self.bus = _OutboundSilencer(original_bus)
            try:
                result = original_fail(
                    self, publish_completion=publish_completion
                )
                if asyncio.iscoroutine(result):
                    await result
            finally:
                if original_bus is not None:
                    self.bus = original_bus

        TurnDelivery.fail = _wrap_fail
        return True, "TurnDelivery.fail patched"
```

---

## Шаг 2. Файл `lib/core/project_settings.py` — добавить модель

Файл: 733 строки, 9 классов.

### 2.1. Класс `ErrorMessagesSettings` (после `class CompactSettings`, ~строка 65, **перед** комментарием `# DuckDbQuerySettings...`)

**Вставить блок**:
```python
class ErrorMessagesSettings(_StrictOptional):
    """Заготовленные ответы при internal-ошибке ``AgentLoop._process_message``.

    Используется патчем ``RuntimePatcher.patch_turn_delivery_fail`` (см.
    спеку ``openspec/specs/runtime/error-fallback``): при любом
    ``Exception`` в upstream-``AgentLoop`` пользователь получает
    ``gateway.error_messages.internal_error`` вместо захардкоженного
    англоязычного литерала из upstream-``TurnDelivery.fail``. Детали
    исключения (тип + текст) пишутся в ``agent_gateway_logs`` при
    ``log_to_db=true``.

    Attributes:
        internal_error: текст, который видит пользователь вместо upstream
            ``"Sorry, I encountered an error."``. По умолчанию — русская
            формулировка без раскрытия внутренних деталей.
        log_to_db: писать ли ``event_type="turn_failed"`` в
            ``agent_gateway_logs`` через ``DbLoggingService.try_log_event``
            (см. ``lib/services/db_logging_service.py:34``). При
            ``False`` — детали остаются только в ``loguru``. По умолчанию
            ``True`` (оператор видит, что сломалось, через
            ``history_search``).

    Unknown keys разрешены (``_StrictOptional(extra="allow")``) —
    forward-compat по будущим per-channel/per-language формулировкам.
    """

    internal_error: str | None = None
    log_to_db: bool | None = None
```

### 2.2. Поле в `GatewaySettings` (строки 126-135)

**Найти** строку с `compact: CompactSettings | None = None` (~строка 131). **Добавить сразу после**:
```python
    error_messages: ErrorMessagesSettings | None = None
```

---

## Шаг 3. Файл `project.json` — добавить закомментированный пример

Файл: 508 строк. Секция `gateway.compact` заканчивается ~строкой 401, `gateway.sync` начинается ~строки 404.

**Найти** строку `"sync": {` (начало секции sync, ~строка 404). **Добавить ПЕРЕД ней**:
```jsonc
    //  error_messages.* — заготовленные ответы при internal-ошибке
    //    AgentLoop._process_message (RuntimePatcher.patch_turn_delivery_fail).
    //    По спеке openspec/specs/runtime/error-fallback. Заменяет
    //    захардкоженный upstream-литерал "Sorry, I encountered an error."
    //    на операторски-редактируемый текст + запись turn_failed
    //    в agent_gateway_logs (если включено).
    //    internal_error  — текст, который видит пользователь вместо
    //                      upstream-литерала. По умолчанию русская
    //                      формулировка без раскрытия внутренних деталей.
    //    log_to_db       — писать ли event_type="turn_failed" в
    //                      agent_gateway_logs (по умолчанию true).
    // "error_messages": {
    //   "internal_error": "Я не справился с вашим вопросом. Попробуйте, пожалуйста, переформулировать конкретнее — например, уточните ключевую часть или приведите пример.",
    //   "log_to_db": true
    // },
```

---

## Шаг 4. Файл `openspec/specs/runtime/error-fallback/spec.md` — НОВЫЙ

Создать каталоги `openspec/specs/runtime/` (в v2.5.3 этот каталог уже есть, но в нём только `context/`).

**Создать файл `openspec/specs/runtime/error-fallback/spec.md`** с содержимым:
```markdown
# Спека: Error fallback messages

## Назначение

Конфигурируемый заготовленный ответ при необработанном исключении в
`AgentLoop._process_message` (upstream-`nanobot`) с записью деталей в
долговечный журнал `agent_gateway_logs`. Заменяет захардкоженный
`"Sorry, I encountered an error."` в `TurnDelivery.fail` на
операторски-редактируемый текст без утечки traceback'а пользователю.

## Requirements

### Requirement: Подмена upstream-литерала

WHEN `AgentLoop._process_message` ловит `Exception` (любое исключение,
кроме `asyncio.CancelledError`) и зовёт `TurnDelivery.fail`,
THEN система SHALL отправить пользователю **ровно один** `OutboundMessage`
с `content` равным `gateway.error_messages.internal_error` из
`project.json` (или default-значению `_DEFAULT_INTERNAL_ERROR_TEXT`,
если секция отсутствует),
AND `metadata._error_kind` SHALL быть равен `"internal"`,
AND никакой upstream-литерал `"Sorry, I encountered an error."`
пользователю отправлен НЕ ДОЛЖЕН быть.

#### Scenario: Секция `error_messages` отсутствует

- **WHEN** в `project.json` нет `gateway.error_messages.internal_error`
- **THEN** пользователь получает текст `_DEFAULT_INTERNAL_ERROR_TEXT`
  (русская формулировка без раскрытия внутренних деталей),
  `outbound.metadata["_error_kind"] == "internal"`

#### Scenario: Секция объявлена с custom-текстом

- **WHEN** в `project.json` указано
  `gateway.error_messages.internal_error = "Сервис временно недоступен."`
- **THEN** пользователь получает именно этот текст

#### Scenario: Неверный тип `internal_error`

- **WHEN** в `project.json` `gateway.error_messages.internal_error`
  имеет тип, отличный от `string` (например, число или массив)
- **THEN** используется default `_DEFAULT_INTERNAL_ERROR_TEXT`
  (fail-open; никакого исключения)

### Requirement: Отличимость от обычного ответа

- **THEN** `outbound.metadata["_error_kind"] == "internal"`
  (отличимо от обычного ответа и от upstream-литерала
  `"Sorry, I encountered an error."`)

### Requirement: Запись в `agent_gateway_logs`

WHEN `TurnDelivery.fail` вызывается из `except Exception`-блока,
THEN при `gateway.error_messages.log_to_db=true` (default) система SHALL
записать в `agent_gateway_logs` запись `event_type="turn_failed"` с
payload, содержащим **ВСЕ** перечисленные ниже поля, полученные
**из авторитетных источников**:

- `session_key` — из атрибута `TurnDelivery.session_key` (установлен
  через `TurnDelivery.create(msg, session_key, ...)` в
  `turn_delivery.py:85-103`). **НЕ** из `lifecycle_message`
  (такого поля нет).
- `sender_id` — из `lifecycle_message.sender_id`.
- `channel` — из `lifecycle_message.channel`.
- `chat_id` — из `lifecycle_message.chat_id`.
- `agent_id` — из `config`, переданный через
  `RuntimePatcher.apply_all` → `patch_turn_delivery_fail`
  (через `_resolve_agent_id(config, agent)`).
- `exception_type` — `type(exc).__name__`, где `exc = sys.exception()`
  (активное исключение из upstream except-блока).
- `exception_message` — `str(exc)`.
- `exception_available` — `True` если `exc is not None`, иначе `False`
  (прямой вызов вне except-блока — для юнит-тестов).
- `failure_error_kind` — snapshot `self._failure_error_kind`
  (private upstream-атрибут, опциональный).

#### Scenario: `log_to_db=true` (default)

- **WHEN** `TurnDelivery.fail` вызывается из `except Exception`-блока,
  `db_logging_service is not None`
- **THEN** запись `event_type="turn_failed"`, `level="ERROR"`,
  `payload` содержит все обязательные поля

#### Scenario: `log_to_db=false`

- **WHEN** в `project.json` `gateway.error_messages.log_to_db=false`
- **THEN** запись в `agent_gateway_logs` НЕ пишется,
  детали остаются только в `loguru`

#### Scenario: `db_logging_service is None`

- **WHEN** `db_logging_service` is `None` (отключённый `logging.db`,
  ещё не стартовал, и т.п.)
- **THEN** запись молча пропускается (fail-open), обёртка не падает

#### Scenario: Полный payload

- **WHEN** `TurnDelivery.fail` вызывается из
  `except ValueError("boom")`-блока, `lifecycle_message.sender_id="u-42"`,
  `TurnDelivery.session_key="s-7"`, `agent_id="agent_main"`,
  `_failure_error_kind="RuntimeError"`
- **THEN** `agent_gateway_logs` получает запись с:
  `event_type="turn_failed"`, `level="ERROR"`,
  `session_id="s-7"`, `user_id="u-42"`, `channel=<lifecycle.channel>`,
  `summary="RuntimeError"`, `payload={kind: "internal",
  failure_error_kind: "RuntimeError", agent_id: "agent_main",
  sender_id: "u-42", chat_id: <lifecycle.chat_id>,
  exception_type: "ValueError", exception_message: "boom",
  exception_available: True}`

### Requirement: Финализация `turn_completed` event

- **THEN** после публикации fallback outbound система SHALL вызвать
  оригинальный `TurnDelivery.fail(self, publish_completion=...)`
  для продолжения логики `turn_completed`
- **AND** на время вызова оригинала `self.bus` подменяется на
  `_OutboundSilencer` (per-instance), чтобы upstream-`fail` НЕ
  опубликовал второй outbound со своим литералом, но
  `turn_completed` runtime-event всё равно ушёл

### Requirement: `turn_completed` outcome

- **THEN** `turn_completed` публикуется с `outcome="failed"` и
  `failure_kind="internal"` (как это делает upstream `TurnDelivery.fail`)

### Requirement: Нет утечки внутренних деталей

- **THEN** `OutboundMessage.content` SHALL содержать **ТОЛЬКО** текст
  `gateway.error_messages.internal_error` (без `str(exc)`, traceback,
  имени функции или module path)

### Requirement: `asyncio.CancelledError` не проходит

- `TurnDelivery.fail` НЕ вызывается для `asyncio.CancelledError` —
  в upstream отдельная ветка `except` с `delivery.abort_stream()`.
  Патч НЕ вмешивается в эту ветку.
```

---

## Шаг 5. Файл `tools/demo_internal_fallback.py` — НОВЫЙ

**Создать файл `tools/demo_internal_fallback.py`** с содержимым:
```python
"""Демонстрация fallback-ответа при internal-ошибке AgentLoop.

Запускать::

    PYTHONIOENCODING=utf-8 python tools/demo_internal_fallback.py

Что показывает:
  1) Реальный OutboundMessage, который получит пользователь (текст, channel, metadata).
  2) Что upstream-литерал "Sorry, I encountered an error." НЕ публикуется.
  3) Что ``turn_completed`` runtime-event всё равно вызывается.
  4) Что запись попадает в ``agent_gateway_logs`` (если есть ``DbLoggingService``).

Скрипт использует stub-``TurnDelivery`` с настоящим Bus и asyncio-циклом,
имитируя except-блок ``AgentLoop._process_message``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanobot.bus.events import InboundMessage, OutboundMessage


class _StubBus:
    def __init__(self) -> None:
        self.published: list[OutboundMessage] = []

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.published.append(msg)


class _RuntimeEventPublisher:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def turn_completed(self, **kw) -> None:
        self.events.append(kw)


class _StubTurnDelivery:
    """Поведение upstream ``nanobot/agent/turn_delivery.py:TurnDelivery.fail``."""

    def __init__(self) -> None:
        self.bus: _StubBus | None = None
        self.session_key: str | None = None
        self.lifecycle_message: InboundMessage | None = None
        self.runtime_event_publisher: _RuntimeEventPublisher | None = None
        self._failure_error_kind: str | None = None

    async def fail(self, *, publish_completion: bool) -> None:
        await self.bus.publish_outbound(  # type: ignore[union-attr]
            OutboundMessage(
                channel=self.lifecycle_message.channel,  # type: ignore[union-attr]
                chat_id=self.lifecycle_message.chat_id,    # type: ignore[union-attr]
                content="Sorry, I encountered an error.",
                metadata=dict(self.lifecycle_message.metadata or {}),  # type: ignore[union-attr]
            )
        )
        if publish_completion:
            await self.runtime_event_publisher.turn_completed(  # type: ignore[union-attr]
                channel=self.lifecycle_message.channel,  # type: ignore[union-attr]
                chat_id=self.lifecycle_message.chat_id,    # type: ignore[union-attr]
                session_key=self.session_key,
                metadata=self.lifecycle_message.metadata,  # type: ignore[union-attr]
                outcome="failed",
                failure_kind="internal",
            )


def _install_stub_turn_delivery() -> types.ModuleType:
    mod = types.ModuleType("nanobot.agent.turn_delivery")
    mod.TurnDelivery = _StubTurnDelivery
    sys.modules["nanobot.agent.turn_delivery"] = mod
    return mod


async def _main() -> None:
    mod = _install_stub_turn_delivery()

    from lib.services.runtime_patcher import RuntimePatcher

    bus = _StubBus()
    publisher = _RuntimeEventPublisher()
    inst = _StubTurnDelivery()
    inst.bus = bus
    inst.session_key = "s-demo-1"
    inst.runtime_event_publisher = publisher
    inst.lifecycle_message = InboundMessage(
        channel="telegram",
        chat_id="chat-1",
        sender_id="u-1",
        text="hello",
        metadata={"request_id": "r-1"},
    )
    inst._failure_error_kind = "RuntimeError"

    patcher = RuntimePatcher()
    settings = None
    ok, msg = patcher.patch_turn_delivery_fail(settings=settings)
    print(f"patch_turn_delivery_fail → ok={ok}, msg={msg!r}")
    if not ok:
        return

    try:
        raise ValueError("boom")
    except ValueError:
        await mod.TurnDelivery.fail(inst, publish_completion=True)

    print()
    print(f"published outbound'ов: {len(bus.published)}")
    for o in bus.published:
        print(f"  - content={o.content!r}")
        print(
            f"    channel={o.channel!r}  chat_id={o.chat_id!r}"
            f"  metadata={json.dumps(o.metadata, ensure_ascii=False)}"
        )
    print()
    print(f"runtime events: {len(publisher.events)}")
    for ev in publisher.events:
        print(f"  - {json.dumps(ev, ensure_ascii=False)}")
    assert len(bus.published) == 1, "ровно один outbound (без двойного)"
    assert "Sorry" not in bus.published[0].content, "upstream-литерал не ушёл"
    assert bus.published[0].metadata.get("_error_kind") == "internal"
    assert bus.published[0].metadata.get("_final_turn") is True
    assert len(publisher.events) == 1, "turn_completed вызван"
    print()
    print("OK: ровно один outbound, upstream-литерал не ушёл, turn_completed вызван.")


if __name__ == "__main__":
    asyncio.run(_main())
```

---

## Шаг 6. Файл `tests/test_project_settings.py` — добавить класс

Файл: 733 строки. **В самый конец** файла (после последнего класса, ~строка 731) вставить:

```python


class TestErrorMessagesSettings:
    """Валидация ``gateway.error_messages.*`` (error fallback)."""

    def test_missing_section_yields_none(self) -> None:
        """Без секции — поле ``error_messages`` равно ``None``."""
        result = ProjectSettings.model_validate({"gateway": {}})
        assert result.gateway.error_messages is None

    def test_empty_section_yields_model_with_nones(self) -> None:
        """Пустая секция — сконструированная модель с ``None``-полями."""
        result = ProjectSettings.model_validate(
            {"gateway": {"error_messages": {}}}
        )
        assert result.gateway.error_messages is not None
        assert result.gateway.error_messages.internal_error is None
        assert result.gateway.error_messages.log_to_db is None

    def test_custom_internal_error(self) -> None:
        """Custom-текст пробрасывается в модель."""
        result = ProjectSettings.model_validate(
            {
                "gateway": {
                    "error_messages": {
                        "internal_error": "Сервис временно недоступен.",
                        "log_to_db": False,
                    }
                }
            }
        )
        em = result.gateway.error_messages
        assert em.internal_error == "Сервис временно недоступен."
        assert em.log_to_db is False

    def test_invalid_internal_error_type_fails_fast(self) -> None:
        """Неверный тип ``internal_error`` → ``ConfigurationError``."""
        with pytest.raises(Exception) as exc_info:
            ProjectSettings.model_validate(
                {"gateway": {"error_messages": {"internal_error": 123}}}
            )
        assert "error_messages" in str(exc_info.value)

    def test_invalid_log_to_db_type_fails_fast(self) -> None:
        """Неверный тип ``log_to_db`` → ``ConfigurationError``."""
        with pytest.raises(Exception) as exc_info:
            ProjectSettings.model_validate(
                {"gateway": {"error_messages": {"log_to_db": [1, 2]}}}
            )
        assert "error_messages" in str(exc_info.value)

    def test_extra_keys_allowed(self) -> None:
        """Forward-compat: неизвестные ключи не падают (extra=allow)."""
        result = ProjectSettings.model_validate(
            {
                "gateway": {
                    "error_messages": {
                        "internal_error": "x",
                        "future_field": "y",
                    }
                }
            }
        )
        assert result.gateway.error_messages.internal_error == "x"
```

---

## Шаг 7. Файл `tests/test_runtime_patcher.py` — добавить хелпер + класс + обновить TestPatchSpecs

Файл: 1228 строк. Три правки.

### 7.1. Обновить `TestPatchSpecs.test_all_patches_have_specs` (строка ~1147)

**Найти** блок (строки 1147-1158):
```python
    def test_all_patches_have_specs(self):
        from lib.services.runtime_patcher import RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        expected = {
            "context_governor", "save_turn", "exec_limits", "exec_timeout_cap",
            "tool_limits", "assemble_outbound", "context_bridge_seed",
            "async_save", "subagent_logging", "project_tools",
            "compact_tracking", "compact_command", "idle_guard",
            "session_content_cleanup", "document_text_threshold",
        }
        assert set(specs) == expected
```

**Заменить** на (убрать `context_bridge_seed` после шага 0, добавить `turn_delivery_fail`):
```python
    def test_all_patches_have_specs(self):
        from lib.services.runtime_patcher import RuntimePatcher

        specs = RuntimePatcher.patch_specs()
        expected = {
            "context_governor", "save_turn", "exec_limits", "exec_timeout_cap",
            "tool_limits", "assemble_outbound",
            "async_save", "subagent_logging", "project_tools",
            "compact_tracking", "compact_command", "idle_guard",
            "session_content_cleanup", "document_text_threshold",
            "turn_delivery_fail",
        }
        assert set(specs) == expected
```

### 7.2. Добавить модульный хелпер `_make_stub_td_module` (перед финальным классом, **в самый конец** файла, строка 1227)

**Вставить блок** (это копия из master `tests/test_runtime_patcher.py:1246-1305`):
```python


def _make_stub_td_module(published, turn_completed_calls):
    """Создать НЕЗАВИСИМЫЙ stub-модуль ``nanobot.agent.turn_delivery``.

    Каждый вызов возвращает СВЕЖИЙ класс ``TurnDelivery`` — критично,
    потому что патч мутирует ``TurnDelivery.fail`` на уровне класса,
    и если использовать общий класс между тестами, состояние протекает.

    Stub воспроизводит upstream ``turn_delivery.py:336-353``:
    ``fail()`` зовёт ``await self.bus.publish_outbound(...)`` (а не
    мутирует общий список в обход bus), чтобы per-instance прокси
    ``_OutboundSilencer`` мог подавить outbound при вызове оригинала.
    """
    import types as _types

    class _RuntimeEventPublisher:
        async def turn_completed(self, **kwargs):
            turn_completed_calls.append(kwargs)

    class _StubBus:
        def __init__(self, sink):
            self._sink = sink

        async def publish_outbound(self, msg):
            self._sink.append(msg)

    class _TurnDelivery:
        def __init__(self):
            self.lifecycle_message = None
            self.bus = None
            self.session_key = None
            self._failure_error_kind = None
            self.runtime_event_publisher = None

        async def fail(self, *, publish_completion: bool) -> None:
            from nanobot.bus.events import OutboundMessage

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=self.lifecycle_message.channel,
                    chat_id=self.lifecycle_message.chat_id,
                    content="Sorry, I encountered an error.",
                    metadata=dict(self.lifecycle_message.metadata or {}),
                )
            )
            if publish_completion:
                await self.runtime_event_publisher.turn_completed(
                    channel=self.lifecycle_message.channel,
                    chat_id=self.lifecycle_message.chat_id,
                    session_key=self.session_key,
                    metadata=self.lifecycle_message.metadata,
                    outcome="failed",
                    failure_kind="internal",
                )

    mod = _types.ModuleType("nanobot.agent.turn_delivery")
    mod.TurnDelivery = _TurnDelivery

    def _make_instance():
        inst = _TurnDelivery()
        inst.lifecycle_message = MagicMock()
        inst.lifecycle_message.channel = "cli"
        inst.lifecycle_message.chat_id = "c1"
        inst.lifecycle_message.metadata = {"foo": "bar"}
        inst.lifecycle_message.sender_id = "u1"
        inst.lifecycle_message.session_key = None
        inst.lifecycle_message.user_id = None
        inst.bus = _StubBus(published)
        inst.session_key = "sess1"
        inst._failure_error_kind = "RuntimeError"
        inst.runtime_event_publisher = _RuntimeEventPublisher()
        return inst

    return mod, _make_instance
```

### 7.3. Добавить класс `TestPatchTurnDeliveryFail` (в самый конец файла, после хелпера)

**Вставить блок** (19 тестов, копия из master `tests/test_runtime_patcher.py:1323-1765`):
```python


class TestPatchTurnDeliveryFail:
    """Контракт error fallback (``openspec/specs/runtime/error-fallback``)."""

    @pytest.fixture
    def stub_td_module(self, monkeypatch):
        """Подменить ``nanobot.agent.turn_delivery`` stub-модулем.

        Каждый вызов фикстуры создаёт СВЕЖИЙ класс ``TurnDelivery`` —
        критично, потому что патч мутирует ``TurnDelivery.fail`` на
        уровне класса, и общий класс между тестами протекал бы.

        Возвращает ``(mod, published, turn_completed_calls, make_instance)``.
        """
        published: list = []
        turn_completed_calls: list = []
        mod, make_instance = _make_stub_td_module(
            published, turn_completed_calls,
        )
        monkeypatch.setitem(sys.modules, "nanobot.agent.turn_delivery", mod)
        return mod, published, turn_completed_calls, make_instance

    @pytest.mark.asyncio
    async def test_default_text_when_no_settings(self, stub_td_module):
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_turn_delivery_fail(settings=None)
        assert ok, msg
        from lib.services.runtime_patcher import (
            _DEFAULT_INTERNAL_ERROR_TEXT,
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        out = published[0]
        assert out.content == _DEFAULT_INTERNAL_ERROR_TEXT
        assert out.channel == "cli"
        assert out.chat_id == "c1"
        assert out.metadata.get("_error_kind") == "internal"
        assert out.metadata.get("_final_turn") is True

    @pytest.mark.asyncio
    async def test_custom_text_from_settings(self, stub_td_module):
        _, published, _, _ = stub_td_module
        settings = {
            "gateway": {
                "error_messages": {
                    "internal_error": "Сервис временно недоступен.",
                },
            },
        }
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_turn_delivery_fail(settings=settings)
        assert ok

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        assert published[0].content == "Сервис временно недоступен."

    @pytest.mark.asyncio
    async def test_no_exception_details_leak_to_user(self, stub_td_module):
        """requirement: content содержит ТОЛЬКО заготовку, не str(exc)."""
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        inst._failure_error_kind = "KeyError: agent_internal_state_xyz"
        await inst.fail(publish_completion=True)

        assert "KeyError" not in published[0].content
        assert "agent_internal_state_xyz" not in published[0].content

    @pytest.mark.asyncio
    async def test_upstream_literal_not_published(self, stub_td_module):
        """Upstream-литерал ``"Sorry, I encountered an error."`` НЕ ДОЛЖЕН
        доходить до пользователя — это инвариант подмены.
        """
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        for msg in published:
            assert msg.content != "Sorry, I encountered an error.", (
                f"upstream literal leaked: {msg.content!r}"
            )

    @pytest.mark.asyncio
    async def test_log_to_db_true_writes_event(
        self, stub_td_module, monkeypatch
    ):
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append((svc, event, producer, event_type))
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        svc = MagicMock()
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings=None, db_logging_service=svc, agent_id="agent_test",
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(recorded) == 1
        svc_arg, log_event, producer, event_type = recorded[0]
        assert svc_arg is svc
        assert producer == "runtime_patcher"
        assert event_type == "turn_failed"
        assert log_event.event_type == "turn_failed"
        assert log_event.session_id == "sess1"
        assert log_event.channel == "cli"
        assert log_event.payload["kind"] == "internal"
        assert log_event.payload["failure_error_kind"] == "RuntimeError"
        assert log_event.payload["agent_id"] == "agent_test"
        assert log_event.payload["sender_id"] == "u1"
        assert log_event.payload["chat_id"] == "c1"
        assert log_event.payload["exception_available"] is False
        assert log_event.payload["exception_type"] is None
        assert log_event.payload["exception_message"] is None

    @pytest.mark.asyncio
    async def test_log_to_db_false_skips_db(
        self, stub_td_module, monkeypatch
    ):
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings={
                "gateway": {"error_messages": {"log_to_db": False}},
            },
            db_logging_service=MagicMock(),
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert recorded == [], (
            f"try_log_event called despite log_to_db=False: {recorded}"
        )

    @pytest.mark.asyncio
    async def test_no_db_logging_service_is_fail_open(
        self, stub_td_module, monkeypatch
    ):
        """При ``db_logging_service=None`` fallback-сообщение всё равно
        уходит пользователю (fail-open).
        """
        _, published, _, _ = stub_td_module

        def fake_try_log_event(svc, event, *, producer, event_type):
            raise AssertionError("try_log_event should not be called")

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings=None,
            db_logging_service=None,
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        assert published[0].content == (
            "Я не справился с вашим вопросом. "
            "Попробуйте, пожалуйста, переформулировать конкретнее — "
            "например, уточните ключевую часть или приведите пример."
        )

    @pytest.mark.asyncio
    async def test_turn_completed_event_published(self, stub_td_module):
        """``publish_completion=True`` — оригинальный ``fail`` зовёт
        ``turn_completed`` (сохранение runtime-event публикации).

        Примечание: kwargs ``outcome``/``failure_kind`` появляются
        только в nanobot >= 0.3.5 (см. ``bus/runtime_events.py``).
        На 0.3.0 их нет — проверяем их условно, не падая.
        """
        _, _, turn_completed_calls, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(turn_completed_calls) == 1
        # outcome/failure_kind есть только в nanobot 0.3.5+
        if "outcome" in turn_completed_calls[0]:
            assert turn_completed_calls[0]["outcome"] == "failed"
        if "failure_kind" in turn_completed_calls[0]:
            assert turn_completed_calls[0]["failure_kind"] == "internal"

    @pytest.mark.asyncio
    async def test_turn_completed_not_published_when_completion_false(
        self, stub_td_module
    ):
        _, _, turn_completed_calls, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=False)

        assert turn_completed_calls == []

    @pytest.mark.asyncio
    async def test_session_key_from_turn_delivery_instance(
        self, stub_td_module, monkeypatch
    ):
        """``session_key`` берётся из ``self.session_key`` (атрибут
        ``TurnDelivery``), а НЕ из ``lifecycle_message`` (такого поля нет).
        """
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()
        inst.lifecycle_message.session_key = "WRONG_LIFECYCLE"
        inst.session_key = "real_session_key"
        await inst.fail(publish_completion=True)

        assert recorded[0].session_id == "real_session_key"

    @pytest.mark.asyncio
    async def test_sender_id_from_lifecycle_message(
        self, stub_td_module, monkeypatch
    ):
        """``sender_id`` берётся из ``lifecycle_message.sender_id``."""
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()
        inst.lifecycle_message.sender_id = "u-42"
        await inst.fail(publish_completion=True)

        assert recorded[0].payload["sender_id"] == "u-42"
        assert recorded[0].user_id == "u-42"

    @pytest.mark.asyncio
    async def test_agent_id_passed_through(
        self, stub_td_module, monkeypatch
    ):
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(
            settings=None, db_logging_service=MagicMock(), agent_id="agent_main",
        )

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert recorded[0].payload["agent_id"] == "agent_main"

    @pytest.mark.asyncio
    async def test_exception_available_inside_except_block(
        self, stub_td_module, monkeypatch
    ):
        """При вызове из ``except``-блока ``sys.exception()`` возвращает
        активное исключение — payload содержит ``exception_available=true``
        и тип/текст исключения.
        """
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()

        try:
            raise ValueError("boom-12345")
        except Exception:
            await inst.fail(publish_completion=True)

        assert len(recorded) == 1
        evt = recorded[0]
        assert evt.payload["exception_available"] is True
        assert evt.payload["exception_type"] == "ValueError"
        assert evt.payload["exception_message"] == "boom-12345"

    @pytest.mark.asyncio
    async def test_exception_unavailable_degrades_gracefully(
        self, stub_td_module, monkeypatch
    ):
        """Без активного исключения (прямой вызов из теста) — payload
        содержит ``exception_available=false`` и ``null`` тип/сообщение.
        """
        _, _, _, _ = stub_td_module
        recorded: list = []

        def fake_try_log_event(svc, event, *, producer, event_type):
            recorded.append(event)
            return True

        monkeypatch.setattr(
            "lib.services.db_logging_service.try_log_event",
            fake_try_log_event,
        )

        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None, db_logging_service=MagicMock())

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(recorded) == 1
        evt = recorded[0]
        assert evt.payload["exception_available"] is False
        assert evt.payload["exception_type"] is None
        assert evt.payload["exception_message"] is None

    def test_module_not_loaded_returns_false(self, monkeypatch):
        """Если ``TurnDelivery`` модуль отсутствует в ``sys.modules`` —
        патч возвращает ``(False, <reason>)`` без падения.
        """
        monkeypatch.delitem(
            sys.modules, "nanobot.agent.turn_delivery", raising=False
        )
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_turn_delivery_fail(settings=None)
        assert not ok
        assert "not loaded" in msg

    def test_turn_delivery_fail_missing_returns_false(self, stub_td_module):
        """Если у stub-класса нет атрибута ``fail`` — патч no-op."""
        stub_td_module[0].TurnDelivery.fail = None  # type: ignore[assignment]
        patcher = RuntimePatcher()
        ok, msg = patcher.patch_turn_delivery_fail(settings=None)
        assert not ok
        assert "missing" in msg

    def test_invalid_internal_error_type_falls_back_to_default(
        self, stub_td_module
    ):
        """Невалидный ``internal_error`` (не строка) → default-текст."""
        _, _, _, _ = stub_td_module
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_turn_delivery_fail(
            settings={
                "gateway": {"error_messages": {"internal_error": 999}},
            },
        )
        assert ok

    @pytest.mark.asyncio
    async def test_cancelled_error_path_untouched(self, stub_td_module):
        """Патч не подменяет ``_process_message`` — CancelledError-ветка
        upstream'а остаётся нетронутой. Здесь фиксируем только инвариант
        «ровно один outbound» и отсутствие побочных эффектов.
        """
        _, published, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        await inst.fail(publish_completion=True)

        assert len(published) == 1
        assert published[0].metadata.get("_error_kind") == "internal"

    @pytest.mark.asyncio
    async def test_bus_is_restored_after_original_fail(
        self, stub_td_module
    ):
        """``self.bus`` восстанавливается после вызова оригинального
        ``fail()`` — критично для следующих вызовов в этом обороте.
        """
        _, _, _, _ = stub_td_module
        patcher = RuntimePatcher()
        patcher.patch_turn_delivery_fail(settings=None)

        inst = stub_td_module[3]()
        real_bus = inst.bus
        await inst.fail(publish_completion=True)

        assert inst.bus is real_bus
```

---

## Шаг 8. Файл `docs/architecture/runtime-patcher-inventory.md`

**Найти таблицу патчей** (в v2.5.3 документирует 15 патчей). **Добавить строку в конец таблицы**:
```markdown
| `turn_delivery_fail` | `nanobot.agent.turn_delivery.TurnDelivery.fail` | medium | medium | [RuntimePatcher.patch_turn_delivery_fail](../../lib/services/runtime_patcher.py) | подмена захардкоженного upstream-литерала + event_type="turn_failed" в agent_gateway_logs |
```

> **Точное место и формат строки** — посмотреть в существующей таблице файла. Колонки: name | target | risk | required | file:link | purpose.

---

## Шаг 9. Bash-скрипт, выполняющий всё одной командой

**Создать файл `apply_v2.5.3_error_fallback.sh`** в корне (или временно):

```bash
#!/usr/bin/env bash
# Применяет error-fallback на release/v2.5.3.
# Предусловие: checkout release/v2.5.3, все файлы закоммичены,
#              nanobot-ai==0.3.0 (АПГРЕЙД НЕ НУЖЕН — см. Шаг 0).
# Использование:
#   git checkout release/v2.5.3
#   bash apply_v2.5.3_error_fallback.sh
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# --- Шаг 0: проверка API (апгрейд nanobot НЕ выполняется) ---
python -c "from nanobot.agent.turn_delivery import TurnDelivery; import inspect; s = inspect.signature(TurnDelivery.fail); assert list(s.parameters.keys()) == ['self', 'publish_completion'], s; print('OK: TurnDelivery.fail API совместим с патчем')"

# --- Шаги 1-8 выполнить вручную через `apply_patch` (см. TASK.md), ---
# --- потому что sed/heredoc плохо читается на больших вставках.    ---

# --- Шаг 9: проверки ---
python -c "from nanobot.agent.turn_delivery import TurnDelivery; import inspect; print(inspect.signature(TurnDelivery.fail))"
python -c "from lib.core.project_settings import ProjectSettings; p = ProjectSettings.model_validate({'gateway': {'error_messages': {'internal_error': 'test'}}}); print(p.gateway.error_messages.internal_error)"

pytest tests/test_project_settings.py::TestErrorMessagesSettings -v
pytest tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail -v
pytest tests/test_runtime_patcher.py::TestPatchSpecs::test_all_patches_have_specs -v

PYTHONIOENCODING=utf-8 python tools/demo_internal_fallback.py
```

> **Шаги 1.1-7.3** (правки `runtime_patcher.py`, `project_settings.py`, `project.json`, `tests/*`) — выполнять через `apply_patch`/`edit_file` по блокам из этого документа. **Heredoc-подход НЕ использовать** — вставки большие, контекстные строки и якоря надёжнее.

---

## Шаг 10. Чек-лист «всё работает»

```bash
# A. nanobot API доступен
python -c "from nanobot.agent.turn_delivery import TurnDelivery; import inspect; print(inspect.signature(TurnDelivery.fail))"
# ожидаем: <Signature (self, *, publish_completion: bool) -> None>

# B. Pydantic-модель валидируется
python -c "from lib.core.project_settings import ProjectSettings; p = ProjectSettings.model_validate({'gateway': {'error_messages': {'internal_error': 'test', 'log_to_db': True}}}); print(p.gateway.error_messages.internal_error)"
# ожидаем: test

# C. Patch применяется без падения
python -c "from lib.services.runtime_patcher import RuntimePatcher; p = RuntimePatcher(); ok, msg = p.patch_turn_delivery_fail(settings=None); assert ok is True, msg; print('OK')"
# ожидаем: OK

# D. Демо показывает корректный результат
PYTHONIOENCODING=utf-8 python tools/demo_internal_fallback.py
# ожидаем: 'OK: ровно один outbound, upstream-литерал не ушёл, turn_completed вызван.'

# E. Все 19 тестов TestPatchTurnDeliveryFail зелёные
pytest tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail -v
# ожидаем: 19 passed

# F. TestErrorMessagesSettings все 6 зелёные
pytest tests/test_project_settings.py::TestErrorMessagesSettings -v
# ожидаем: 6 passed

# G. TestPatchSpecs.test_all_patches_have_specs зелёный (число патчей = 15)
pytest tests/test_runtime_patcher.py::TestPatchSpecs::test_all_patches_have_specs -v
# ожидаем: 1 passed

# H. apply_all без failed (нужны mock'и agent/db_logging_service, см. TestApplyAll.test_normal_scenario_no_failures)
pytest tests/test_runtime_patcher.py::TestApplyAllFailed::test_normal_scenario_no_failures -v
# ожидаем: 1 passed
```

Если **все 8 пунктов** проходят — фича работает на `release/v2.5.3`.

---

## Граница задачи (что НЕ делать)

- **НЕ менять** уже существующие патчи в `runtime_patcher.py` кроме явно указанного (удаление `context_bridge_seed`).
- **НЕ редактировать** `db_logging_service.py` — `try_log_event` уже есть.
- **НЕ снимать** закомментированный пример в `project.json` в рабочей копии.
- **НЕ коммитить и НЕ пушить** — это задача владельца после прогона чек-листа.
- **НЕ выполнять** шаг 9 (bash-скрипт) до того, как шаги 1-8 внесены вручную (скрипт только для финальной проверки).
