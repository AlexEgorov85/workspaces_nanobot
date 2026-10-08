# Tasks: startup-vector-preload-gate

## 1. Сервис гейта

- [x] 1.1 `lib/services/startup_gate.py`: `VectorPreloadConfig`,
      `read_vector_preload_config`, `StartupGateReport`, `StartupGate`
      с шагами `wait_for_cache()` / `load_vectors()` / `prepare()`.
- [x] 1.2 Фазы `pending → cache_ready → vectors_ready | unavailable | skipped`,
      `is_awaiting()` и `detail()` для readiness.
- [x] 1.3 Политика `on_unavailable`: `warn` (дефолт) и `fail`
      (`StartupGateError` c приложенным отчётом). Без ретраев и таймаутов.
- [x] 1.4 `ThreadSafeSignal` — сигнал из worker-треда будит ожидающий loop
      через `call_soon_threadsafe`, loop привязывается в `wait()`.
- [x] 1.5 Событие `startup_vector_preload` в `agent_gateway_logs`
      (`INFO` / `WARN`) через `try_log_event`.

## 2. Конфигурация

- [x] 2.1 `lib/core/project_settings.py`: `StartupVectorPreloadSettings`
      (`enabled`, `await_ready`, `on_unavailable`) в `StartupSettings`,
      запись в `__all__`.
- [x] 2.2 `project.json`: документированная секция
      `gateway.startup.vector_preload` с дефолтами кода.
- [x] 2.3 Неизвестная политика и нечисловые значения → безопасные дефолты +
      предупреждение, без падения старта.

## 3. Composition root и readiness

- [x] 3.1 `ApplicationContext.create()`: `ctx.startup_gate` через
      `_make_startup_gate` (создаётся всегда, даже без аудита).
- [x] 3.2 `ctx.cache_ready_signal` / `ctx.background_vector_preload` —
      публикуются entrypoint'ом.
- [x] 3.3 readiness-компонент `vector_search`: `DOWN` + фаза гейта, пока
      векторы не готовы (late binding — регистрация раньше создания).

## 4. Entry point

- [x] 4.1 `gateway.py`: сигнал готовности — `ThreadSafeSignal`.
- [x] 4.2 `_wrapped()`: `publish()` **до** выставления сигнала.
- [x] 4.3 `_run_startup_preparation()` (синхронная обёртка) +
      `_run_startup_preparation_async()` (async-тело) — до `run_forever`.
- [x] 4.4 `_run(ctx)`: каналы стартуют без фазы подготовки; фоновый режим
      (`await_ready=false`) запускает preload в рабочем цикле.
- [x] 4.5 `main()`: `StartupGateError` → `FATAL` + `exit 2`.
- [x] 4.6 `_print_gate_report()` — печать отчёта в терминал.

## 5. Тесты

- [x] 5.1 `tests/test_startup_gate.py`: конфиг, обе фазы, обе политики,
      наблюдаемость (событие/callback), устойчивость к отсутствию
      сервисов.
- [x] 5.2 Guard «без таймаутов» по AST + проба подсаженным дефектом +
      проверка, что имя `wait_for_cache` ложно не срабатывает.
- [x] 5.3 `tests/test_gateway_startup_gate.py`: порядок
      `cache.connect → sync.start → cache.publish → vector.preload → channels`,
      пробуждение из чужого потока, режимы `enabled`/`await_ready`,
      политики `warn`/`fail`, boundary `exit 2`.
- [x] 5.4 `tests/test_application_context.py`: гейт создаётся и связан с
      `preload_service` / `cache_provider`; фикстура fake-модулей дополнена
      пакетом `nanobot.agent.tools` (nanobot 0.3.5) — это же чинит 8
      ранее падавших тестов файла.

## 6. Документация

- [x] 6.1 `AGENTS.md`: `startup_gate.py` в карте проекта + раздел
      Configuration про `gateway.startup.vector_preload.*`.
- [x] 6.2 `CHANGELOG.md` → `[Unreleased] / Added`.
- [x] 6.3 Спека-дельта в `specs/runtime/startup-vector-preload-gate/`.

## Вне области

- [ ] CLI (`cli_agent.py`) не ждёт готовности: у него нет очереди вопросов.
      Если появится CLI-режим с каналами — переиспользовать `StartupGate`.
- [ ] Запись компонента в `openspec/specs/COMPONENTS.md` — только после
      архивации change'а (правило реестра).