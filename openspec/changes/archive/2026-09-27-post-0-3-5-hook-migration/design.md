## Context

Upstream `nanobot-ai==0.3.5` (см. `openspec/changes/nanobot-035-upgrade`) изменил lifecycle-протокол `AgentHook`, формат usage (`LLMUsage` dataclass), shutdown API (`aclose` вместо `close_mcp`), и сигнатуру `_save_turn`. Текущие `lib/hooks/*` хуки проекта унаследованы от nanobot 0.3.0 API, и при попытке использовать новый upstream возвращают `AttributeError` на каждом callback'е (ранее это глоталось `_for_each_hook_safe` с логированием ERROR).

Правки в `lib/hooks/*` для совместимости выполнены локально (без OpenSpec change), и этот документ оформляет их как формальный архитектурный change с дельтами в specs/.

## Goals / Non-Goals

**Goals:**

- Оформить ранее выполненные правки `lib/hooks/*` как формальный change с OpenSpec-дельтой.
- Зафиксировать нормативный контракт: все `lib/hooks/*` хуки ДОЛЖНЫ наследовать `AgentHook`.
- Зафиксировать адаптер `LLMUsage → dict` через `_usage_to_dict()`.
- Обновить `runtime/context` спек: lifecycle хуков — это часть runtime-контракта.

**Non-Goals:**

- Локальный фикс обрезания ответа от провайдера `api.minimax.io` (диагностирован как upstream-проблема).
- Изменение payload `DbLoggingService.log_event` или формата БД-логов.
- Дополнительные хуки (RateLimit, CostLimit и т.п.).
- Рефакторинг `AgentHook`-наследования для существующих плагинов в `workspace/hooks/` (например, `recent_files_hook.py`, `session_file_redirect_hook.py`) — они уже наследуют `AgentHook` через `AgentHook` или работают без callback-методов.

## Decisions

### Decision 1: Хуки наследуют `AgentHook` через прямой base class, не через mixin

**Выбор:** `class DatabaseLoggingHook(AgentHook): ...`

**Альтернативы:**

- Mixin через `class _AgentHookMixin: ...` — отвергнуто: добавляет лишний слой индирекции, не даёт выигрыша, требует рефакторинга всех хуков разом.
- Динамическое подмешивание через `__init_subclass__` — отвергнуто: слишком магично, затрудняет отладку.

**Обоснование:** прямой base class — стандартный Python-паттерн, легко читается, IDE/autocomplete работают. Все три хука уже реализованы так.

### Decision 2: `_usage_to_dict()` возвращает per-turn dict через `to_turn_dict()`

**Выбор:** в `database_logging_hook.py`:

```python
def _usage_to_dict(usage: Any) -> dict | None:
    if usage is None: return None
    if isinstance(usage, dict): return dict(usage) if usage else None
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
```

**Альтернативы:**

- Использовать `usage.to_turn_dict()` напрямую без адаптера — отвергнуто: legacy-путь (dict) сломается; типы не унифицированы.
- Заворачивать в `dataclasses.asdict(usage)` — отвергнуто: `asdict` deep-converts и копирует nested структуры; для ЛЛМ-логирования это лишний overhead.

**Обоснование:** адаптер скрывает разницу между `LLMUsage` dataclass (0.3.5+) и legacy dict; единый путь для всех потребителей (`after_iteration`, `_store_iteration_usage`, `_make_run_event`, `_print_llm_tokens`).

### Decision 3: `aclose()` вместо `close_mcp()` в shutdown-блоках

**Выбор:** `await agent.aclose()` в `gateway.py:334`, `lib/cli/console_loop.py:281`, `benchmarks/runner.py:728`.

**Альтернативы:**

- Try/except `AttributeError` — отвергнуто: маскирует будущие регрессии upstream.
- Wrapper-метод в `lib/lifecycle/shutdown_coordinator.py` — отвергнуто: единый актор shutdown ещё не существует, и три вызова не дублируются в каждом entrypoint.

**Обоснование:** `aclose()` — единственный публичный API shutdown в 0.3.5; миграция тривиальна (1:1 замена).

### Decision 4: `patch_save_turn._wrap` принимает kwargs через `**`-разворот + явные defaults

**Выбор:** сигнатура `def _wrap(session, messages, skip, *, turn_latency_ms=None, summary_checkpoint=None, input_persisted_early=False): ...`

**Альтернативы:**

- `**kwargs` без явных имён — отвергнуто: теряется type-info, IDE не подсказывает.
- Свой dataclass `SaveTurnArgs` — отвергнуто: добавляет indirection без выигрыша (3 поля).

**Обоснование:** явные kwargs с defaults полностью forward-compatible: новые kwargs в nanobot будут падать с `TypeError` снова, но мы их добавим в следующей итерации.

### Decision 5: Allowlist для `workspace/hooks/*` плагинов

**Выбор:** `_allowed_hook_names()` возвращает frozenset с фиксированным списком (см. `openspec/changes/post-0-3-5-patches-cleanup` § 7.3).

**Альтернативы:**

- Полное сканирование всех `.py` файлов — отвергнуто: уже было раньше, привело к попаданию `ActiveFilesHook` и других dead-code в продакшен.
- Per-hook capability declaration (`# allowlist: yes`) в docstring — отвергнуто: магично, сложно поддерживать.

**Обоснование:** allowlist в коде явно показывает, какие плагины прошли ревью; добавление нового плагина = правка `_allowed_hook_names()` = review opportunity.

### Decision 6: `debug_stream_diag.py` помечен REMOVED, но временно оставлен

**Выбор:** удалить `workspace/hooks/debug_stream_diag.py` и запись `"debug_stream_diag"` в `_allowed_hook_names()` сразу после подтверждения диагноза (обрезание — upstream-проблема провайдера).

**Альтернативы:**

- Оставить как постоянный диагностический хук за allowlist-флагом — отвергнуто: лишний код в runtime, не нужен в проде.
- Удалить без оформления change — отвергнуто: нарушает политику проекта (нетривиальная правка → OpenSpec change).

**Обоснование:** диагностический хук выполнил задачу (подтвердил upstream-причину обрезания); удаление — hygiene. Оформлено как REMOVED в proposal и tasks.

## Risks / Trade-offs

- [Risk] `AgentHook`-наследование может сломать будущие хуки, если upstream добавит обязательные абстрактные методы. → [Mitigation] Базовые no-op методы уже покрывают весь lifecycle; при добавлении обязательных методов — открывается отдельный change.
- [Risk] `_usage_to_dict()` молча возвращает `None` для неизвестного типа usage. → [Mitigation] loguru логирует warning в `after_iteration`/`_make_run_event`; неудача не падает в runtime.
- [Risk] `aclose()` без параметров может не сделать cleanup для всех ресурсов (например, MCP-серверы). → [Mitigation] upstream отвечает за корректность shutdown; если ресурсы не освобождаются — это upstream-bug.
- [Trade-off] Allowlist делает добавление плагина явной операцией (правка кода), а не автоматической. Это правильно для безопасности, но добавляет friction для новых плагинов.

## Migration Plan

1. Применить OpenSpec change через `openspec.cmd apply post-0-3-5-hook-migration` (когда задачи будут готовы).
2. После apply — коммит `fix+refactor(hooks): post-0-3-5-hook-migration — AgentHook inheritance, LLMUsage adapter, aclose, save_turn kwargs` (один коммит, либо серия если ревью требует).
3. Удалить `workspace/hooks/debug_stream_diag.py` отдельным коммитом `chore(hooks): удалить debug_stream_diag после диагностики обрезания`.
4. Rollback: `git revert <commit-sha>` — все правки self-contained, откат безопасен.

## Open Questions

- Нет открытых вопросов. Все архитектурные решения приняты; upstream-баг с обрезанием ответа документирован, но фикс на нашей стороне не оправдан (overlap=0 в большинстве случаев, эвристики дают false-positive).
