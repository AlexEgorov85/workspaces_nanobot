# observability/usage-store Specification

## Purpose
Определяет нормативный контракт использования upstream
`nanobot.llm_usage.store.LLMUsageStore` для записи content-free
metadata-only LLM usage (provider, model, duration_ms,
token counts, finish_reason). Стор — SQLite WAL, инициализируется
в `ApplicationContext` и подключается через observer-pipeline
(`provider.set_llm_call_observer(store.record)` или эквивалентный
hook в nanobot 0.3.5). Контракт фиксирует раздельную
семантику с `DbLoggingService`: `LLMUsageStore` — content-free
per-call metadata, `DbLoggingService` — content-rich audit-trail.

## Scope

`agent` — хранилище usage создаёт библиотека nanobot, обвязка — агентская
Реализация: `lib/core/agent_factory.py::_wrap_provider_snapshot_loader`

## Requirements

### Requirement: LLMUsageStore подключается через observer-pipeline

`ApplicationContext` MUST инстанциировать
`nanobot.llm_usage.store.LLMUsageStore(path)` (где `path`
указывает на SQLite WAL-файл в `get_runtime_subdir("usage")`
или иной документированный runtime-путь) и MUST подключить
его через `provider_snapshot_loader` callback в nanobot
0.3.5. Это MUST быть реализовано через обёртку
`wrap_provider_snapshot_loader(base_loader, store)`, которая
для каждого загруженного `ProviderSnapshot` вызывает
`snapshot.provider.set_llm_call_observer(store.record)`.
Для `FallbackProvider` дополнительно вызывается
`snapshot.provider.set_fallback_model_observer(...)` (по
образцу `nanobot.cli.gateway_runtime._observe_provider`).

Подключение MUST происходить ДО первого LLM-вызова (через
передачу обёрнутого loader'а в `AgentLoop.from_config(...)`
в `ApplicationContext.start()`).

Обоснование механизма: проект использует кастомный
`gateway.py` и `ApplicationContext`, а не upstream CLI
`nanobot.cli.gateway_runtime`, где observer подключается
автоматически. `AgentLoop.from_config(...)` создаёт
`provider` внутри и не возвращает его наружу, поэтому
единственный стабильный путь подключения —
оборачивание `provider_snapshot_loader` callback'а,
который `AgentLoop` вызывает сам для получения
`ProviderSnapshot`.

#### Scenario: UsageStore записывает LLM call metadata

- **WHEN** LLM provider завершает вызов с metadata
  `{started_at_ms, duration_ms, provider, model, source,
  stream, finish_reason, usage, error_status_code, error_kind}`
- **THEN** observer-pipeline вызывает `store.record(LLMCallRecord(...))`
  и запись попадает в SQLite WAL-файл.
- **AND** `DbLoggingService` НЕ получает отдельную запись
  `event_type="llm_usage"` с тем же payload (это разные
  concerns — LLMUsageStore хранит metadata, DbLoggingService —
  content-rich события, к которым usage не относится).

#### Scenario: UsageStore недоступен — LLM-вызов продолжается

- **WHEN** `LLMUsageStore.record(...)` бросает исключение
  (например, SQLite file locked, disk full)
- **THEN** observer-pipeline MUST проглотить исключение
  и залогировать WARNING через `logger.warning(...)`.
- **AND** LLM-вызов MUST НЕ прерываться (usage tracking —
  observability concern, не business-critical).

#### Scenario: Подключение через wrap_provider_snapshot_loader

- **WHEN** `ApplicationContext` создаёт `AgentLoop` через
  `from_config(...)` и передаёт
  `provider_snapshot_loader=wrap_provider_snapshot_loader(base, store)`
- **THEN** при первом вызове loader'а (внутри `AgentLoop`)
  обёртка загружает `ProviderSnapshot` через `base(...)`
  и ВЫЗЫВАЕТ `snapshot.provider.set_llm_call_observer(store.record)`
  ДО возврата снапшота.
- **AND** если `snapshot.provider` — `FallbackProvider`,
  обёртка дополнительно вызывает
  `snapshot.provider.set_fallback_model_observer(...)`.
- **AND** при последующих вызовах loader'а (lazy reload
  или смена model preset) обёртка повторно подключает
  observer для нового снапшота.

#### Scenario: Проект не использует nanobot.cli.gateway_runtime

- **WHEN** разработчик читает спеку и ищет, где происходит
  автоматическое подключение `record_llm_call`
- **THEN** в коде проекта (`lib/`, `workspace/`) НЕТ
  вызовов `nanobot.cli.gateway_runtime._observe_provider`
  или прямых вызовов `set_llm_call_observer` на
  `provider`-объекте (потому что `AgentLoop` не возвращает
  `provider` наружу).
- **AND** подключение происходит через
  `wrap_provider_snapshot_loader` в `ApplicationContext`,
  как описано в предыдущем сценарии.
- **AND** контрактный тест `tests/contract/test_llm_observer_api.py`
  фиксирует `LLMCallObserver` type alias и fail-open
  семантику `record_llm_call` upstream'а.

#### Scenario: Fail-soft при ошибке attach observer

- **WHEN** `wrap_provider_snapshot_loader` вызывает
  `set_llm_call_observer(store.record)` и метод бросает
  исключение (теоретический случай; на практике upstream
  просто присваивает поле)
- **THEN** обёртка логирует WARNING через `loguru`:
  `"LLMUsageStore observer failed to attach: {exc}"`
- **AND** возвращает `ProviderSnapshot` БЕЗ observer —
  агент продолжает работать, теряется только telemetry.
- **AND** метрика `llm_observer_attached=0` экспортируется
  в `RuntimeHealth.get_stats()` (см.
  `lib/services/runtime_health.py`).
- **AND** `ApplicationContext.start()` НЕ падает с
  необработанным исключением.

#### Scenario: FallbackProvider observer fan-out

- **WHEN** `snapshot.provider` — `FallbackProvider`
- **THEN** `FallbackProvider.set_llm_call_observer(observer)`
  пробрасывает observer в `self._primary` (через
  `super().set_llm_call_observer(observer)` +
  `self._primary.set_llm_call_observer(observer)`).
- **AND** при активации secondary (`self._provider_factory(fallback)`
  внутри fallback-flow) `fallback_provider.set_llm_call_observer(
  self._llm_call_observer)` вызывается явно (подтверждено
  через интроспекцию `nanobot/providers/fallback_provider.py`).
- **AND** это значит, что **observer корректно propagate
  на active secondary в момент фейловера**, не только
  на момент attach.
- **AND** контрактный тест `tests/contract/test_llm_observer_api.py
  ::test_fallback_provider_propagates_observer` проверяет
  оба случая:
  - `provider._primary.observer == store.record`
    после attach;
  - `secondary.observer == store.record` после создания
    secondary через `provider._provider_factory(...)`.

### Requirement: Content-free контракт LLMCallRecord

`LLMUsageStore` MUST использоваться строго для content-free
metadata-only записей согласно upstream-контракту
(`LLMCallRecord` имеет поля `started_at_ms`, `duration_ms`,
`provider`, `model`, `source`, `stream`, `finish_reason`,
`usage`, `error_status_code`, `error_kind` — НЕ
`messages`, `response_text`, `reasoning_content`,
`tool_payloads`). Запрос на расширение `LLMCallRecord`
новыми content-полями ЗАПРЕЩЁН в рамках этого change —
это нарушение upstream-контракта.

#### Scenario: LLMCallRecord не содержит content

- **WHEN** observer-pipeline формирует `LLMCallRecord`
  из результата LLM-вызова
- **THEN** поля `started_at_ms`, `duration_ms`, `provider`,
  `model`, `source`, `stream`, `finish_reason`, `usage`
  MUST присутствовать.
- **AND** поля `prompt_messages`, `response_text`,
  `reasoning_content`, `tool_payloads` MUST NOT
  присутствовать (эти данные хранятся в `Session.messages`).

#### Scenario: usage_payload использует upstream API

- **WHEN** WebUI запрашивает usage charts
  (`store.usage_payload(days=N)`)
- **THEN** данные читаются через upstream API
  `LLMUsageStore.usage_payload(...)` /
  `recent_calls(...)`.
- **AND** НЕ выполняется прямой SQL `SELECT` в SQLite-файл
  usage-стора из нашего кода (только upstream API).

### Requirement: Single-writer per layer в usage tracking

`LLMUsageStore` MUST быть единственным writer'ом
metadata-only LLM usage. `DbLoggingService` НЕ ДОЛЖЕН
принимать на себя функции usage-стора и наоборот.
Разделение concerns:

- `LLMUsageStore` — per-call metadata для UI-графиков и
  cost-tracking (SQLite WAL, content-free).
- `DbLoggingService` — content-rich audit-trail для
  `data.history_search` и observability (`agent_gateway_logs` в PG,
  content-rich с payload).

Параллельная запись в оба стора ЗАПРЕЩЕНА для одного и того
же `event_type="llm_usage"` — это должно быть либо metadata
в `LLMUsageStore` (предпочтительно), либо content-rich в
`DbLoggingService`, не оба одновременно.

#### Scenario: нет двойной записи llm_usage

- **WHEN** LLM provider завершает вызов
- **THEN** upstream observer-pipeline записывает
  `LLMCallRecord` в `LLMUsageStore`.
- **AND** `DbLoggingService.log_event(LogEvent(event_type="llm_usage", ...))`
  НЕ вызывается параллельно — content-rich version того же
  вызова уже доступна через `Session.messages` upstream.

#### Scenario: DbLoggingService не эмитит llm_usage

- **WHEN** `DbLoggingService` пишет structured event
- **THEN** `event_type="llm_usage"` MUST NOT использоваться
  для content-free metadata-only записей.
- **AND** если нужно записать content-rich вариант
  (например, tool-call с metadata о LLM-вызове) —
  используется `event_type="tool_call"` / `event_type="llm_call"`
  с полным payload согласно спеке `observability/logging-db`.

### Requirement: Контрактные тесты на LLMUsageStore API

Использование upstream `LLMUsageStore` MUST быть покрыто
контрактными тестами, фиксирующими публичный API nanobot
0.3.5: конструктор `(path: Path)`, методы `record(LLMCallRecord)`,
`record_many(iterable)`, `recent_calls(limit)`,
`usage_payload(days, timezone_name, now)`,
`count()` и `close()`. Тесты MUST запускаться первыми при
апгрейде upstream и MUST падать, если upstream меняет
совместимый API.

#### Scenario: LLMUsageStore конструктор принимает Path

- **WHEN** контрактный тест проверяет
  `inspect.signature(LLMUsageStore.__init__)`
- **THEN** сигнатура MUST быть `(self, path: Path) -> None`.
- **AND** тест MUST падать при изменении сигнатуры.

#### Scenario: LLMUsageStore.record принимает LLMCallRecord

- **WHEN** контрактный тест проверяет
  `inspect.signature(LLMUsageStore.record)`
- **THEN** параметр MUST называться `call` и иметь тип
  `LLMCallRecord`.
- **AND** `LLMCallRecord` MUST быть `@dataclass(frozen=True, slots=True)`
  с полями согласно upstream-контракту.

### Requirement: Конфигурация UsageStore

Путь к SQLite-файлу `LLMUsageStore` MUST быть
конфигурируемым через `config.json::gateway.usage_store.sqlite_path`
(опционально, дефолт — `<get_runtime_subdir("usage")>/usage.db`).
Никаких других ключей конфигурации для `LLMUsageStore` в
рамках этого change: размер retention, WAL-mode,
synchronous-уровень управляются upstream (на момент 0.3.5 —
WAL включён, `MAX_CALLS_RETAINED` и `MAX_DAYS_RETAINED`
hardcoded в `nanobot.llm_usage.store`).

#### Scenario: дефолтный путь UsageStore

- **WHEN** `ApplicationContext.start()` создаёт
  `LLMUsageStore` и `gateway.usage_store.sqlite_path`
  не задан
- **THEN** используется путь
  `get_runtime_subdir("usage") / "usage.db"`.
- **AND** родительский каталог создаётся через `ensure_dir`.

#### Scenario: явный путь UsageStore

- **WHEN** `gateway.usage_store.sqlite_path` задан в
  `config.json` (например, `"~/.cache/nanobot/usage.db"`)
- **THEN** `LLMUsageStore` создаётся по этому пути.
- **AND** путь MUST расширяться через `Path(...).expanduser()`
  для поддержки `~`.

## Responsibility

Спека отвечает за **подключение и разграничение**, а не за хранение:

1. инстанцировать `LLMUsageStore` в `ApplicationContext` по
   конфигурации;
2. подключить его к провайдеру LLM через observer-pipeline так, чтобы
   подключение произошло **до** первого вызова;
3. не дать двум слоям писать об одни и те же данные: `LLMUsageStore`
   пишет content-free метаданные, `DbLoggingService` — content-rich
   журнал.

Владелец по `## Scope` — `agent`. Ключевое, что следует из чтения кода:
**собственной реализации у предмета нет**. Класс `LLMUsageStore`, схема
таблицы, `WAL`-режим, retention и `record()` целиком принадлежат
upstream-библиотеке `nanobot.llm_usage.store`. Проект владеет только
обвязкой: фабрикой и подпиской.

Проверено по коду: поиск по `lib/` и `workspace/` на `LLMUsageStore`
находит два места — `lib/core/agent_factory.py` (подписка) и
`lib/core/application_context.py` (создание). Никакого собственного
класса, миграции схемы или SQL в проекте нет.

## Boundary

**Граница — обвязка подключения.** Всё, что внутри библиотеки, предмет не
владеет и не меняет.

Внутри границы:

- `lib/core/application_context.py::_make_usage_store` (строка 1950) —
  создание синглтона по конфигурации;
- `lib/core/agent_factory.py::AgentFactory._wrap_provider_snapshot_loader`
  (строка 286) — обёртка загрузчика snapshot'а, подписывающая observer'ов;
- `config.json → gateway.usage_store` — единственная секция конфигурации.

Вне границы:

- **схема SQLite-таблицы, WAL, retention** — целиком upstream
  (`nanobot.llm_usage.store`: `MAX_CALLS_RETAINED`, `MAX_DAYS_RETAINED`);
- **содержание `LLMCallRecord`** — контракт библиотеки; проект его не
  формирует и не валидирует;
- **`DbLoggingService`** — соседний, но отдельный слой; им владеет спека
  `observability/logging-db`, а поиск по тому же журналу —
  `interfaces/tools-history-search`. Смешивать их записи запрещено
  (см. `## Invariants`);
- **`FallbackProvider` и `set_fallback_model_observer(bus)`** — подписка
  на смену модели, а не учёт usage. Она соседствует в том же месте
  кода, но предметом этой спеки не является;
- **`runtime_health`** — читает счётчики, не владеет ими.

## Public Contract

Публичный контракт предмета — **не класс, а три наблюдаемых факта**.

**1. Точка подключения** — единственная и она в коде одна:

```python
provider.set_llm_call_observer(usage_store.record)
```

`lib/core/agent_factory.py:325`. Обёртка вызывается на каждой загрузке
snapshot'а провайдера, поэтому подписка происходит и на смене модели, а
не только на старте.

**2. Сигнатуры upstream-API**, на которые спека опирается (проверено
живым `inspect.signature`):

| Символ | Фактическая сигнатура | Где проверяется |
|---|---|---|
| `get_llm_usage_store` | `(path: Path | None = None) -> LLMUsageStore` | `tests/contract/test_usage_store_api.py` |
| `LLMUsageStore.__init__` | `(self, path: Path) -> None` | `tests/contract/test_usage_store_api.py` |
| `LLMUsageStore.record` | `(self, call: LLMCallRecord) -> None` | `tests/contract/test_usage_store_api.py` |

То есть требование «сигнатура MUST быть `(self, path: Path) -> None`»
(`Requirements`, строки 222-227) выполняется библиотекой, а не нашим
кодом; наш код лишь **опирается** на него, поэтому и держит контрактный
тест.

**3. Место в сборке:** обёртка передаётся в
`AgentLoop.from_config(config, bus, **kwargs)` как
`provider_snapshot_loader` (`lib/core/agent_factory.py:274-277`) — и
только так, поскольку `AgentLoop` создаёт провайдер внутри
`from_config(...)` и наружу его не отдаёт
(`lib/core/agent_factory.py:293-295`).

Обратите внимание на `None`-семантику обёртки: если у `config` нет
`build_provider_snapshot`, возвращается `None`
(`lib/core/agent_factory.py:312-314`) и используется upstream-дефолт —
то есть подписка не происходит, и это штатный режим, а не отказ.

## Inputs

Входов данных у предмета нет в смысле пользовательского ввода: он
**поглощает** результаты LLM-вызовов, а не обслуживает запросы.

Входы:

1. **Конфигурация** — секция `gateway.usage_store`
   (`config.json:704-706`: `{"enabled": true}`; ключ `sqlite_path` в
   рабочем конфиге не задан);
2. **`LLMCallRecord`** — на вход `LLMUsageStore.record(call)`, формируется
   upstream-провайдером. Проект его не конструирует и не фильтрует;
3. **snapshot провайдера** — `base_loader(preset_name=..., **kwargs)`,
   возвращающий объект с полем `provider`
   (`lib/core/agent_factory.py:317-322`).

Выход для наблюдателя — пустой (None): `record()` ничего не возвращает
(`(self, call: LLMCallRecord) -> None`).

## Outputs

Выход один и он персистентный: **строки usage-метаданных в SQLite-файле
WAL**.

Содержание строки content-free и задано upstream-контрактом
`LLMCallRecord`: провайдер, модель, длительность, счётчики токенов,
`finish_reason`. Проект не читает эти строки обратно и не строит по ним
отчётов — он только пишет.

Асимметрия, которую стоит понимать при чтении спеки: **пишет — предмет,
читает — кто-то другой**. В проекте нет ни запроса к usage-таблице, ни
инструмента, который бы её показывал. Проверено поиском по `lib/` и
`workspace/`: потребителей строк usage там нет.

Побочный выход, который виден в логе: предупреждения
`"LLMUsageStore observer failed to attach: {}"`
(`lib/core/agent_factory.py:327-329`) и
`"LLMUsageStore unavailable ({}); observer will not be attached"`
(`lib/core/application_context.py:1985-1987`).

## State

Состояние у предмета есть, и оно целиком **принадлежит upstream-объекту**:
SQLite-база в режиме WAL, одноимённый файл по пути из конфигурации либо
дефолтному.

Что важно для читателя спеки:

- **наш код не создаёт схему, таблицы и индексы.** Нет ни миграции, ни
  `CREATE TABLE` — проверено поиском по дереву. Всё это делает
  `nanobot.llm_usage.store` при первом открытии;
- **наш код не управляет retention.** `MAX_CALLS_RETAINED` и
  `MAX_DAYS_RETAINED` — модульные константы библиотеки
  (`nanobot/llm_usage/store.py`), и предмет к ним не прикасается. Это
  прямо оговорено в `Requirements` (строки 243-247);
- **дефолтный путь принадлежит библиотеке.** Если `sqlite_path` не задан,
  вызывается `get_llm_usage_store()` без аргумента
  (`lib/core/application_context.py:1977-1978`), и путь выбирает синглтон
  библиотеки;
- **явный путь — наша ответственность**, потому что синглтон его не
  знает: `Path(str(raw_path)).expanduser()` и создание родительского
  каталога (`lib/core/application_context.py:1980-1981`).

Второй элемент состояния — **счётчик досылок** в
`lib/hooks/mcp_identity_hook.py` (`_generated_request_ids`). К предмету
этой спеки он не относится: другой компонент, другое состояние; здесь он
упомянут, чтобы читатель не принял его за часть usage-слоя.

## Dependencies

Прямые:

- `nanobot.llm_usage.get_llm_usage_store` — фабрика-синглтон; импортируется
  **лениво**, внутри `_make_usage_store`
  (`lib/core/application_context.py:1973`);
- `nanobot.llm_usage.store.LLMUsageStore` — сам объект;
- `nanobot.llm_usage.LLMCallRecord` — тип входа `record()`;
- `nanobot.providers.fallback_provider.FallbackProvider` — ленивый импорт
  ради проверки `isinstance` перед второй подпиской
  (`lib/core/agent_factory.py:332-335`);
- `nanobot.agent.loop.AgentLoop` — принимает `provider_snapshot_loader`
  в `from_config(...)`;
- `lib/core/config_service` — чтение `gateway.usage_store`
  (`lib/core/application_context.py:1965-1969`).

Принципиальная оговорка: `nanobot.llm_usage` — **единственная** точка
связи со storage-подсистемой. Агент не импортирует `sqlite3` ради usage
и не держит собственного драйвера.

## Configuration

Единственная секция — `gateway.usage_store`, разбирается в
`UsageStoreSettings` (`lib/core/project_settings.py:143-154`):

| Ключ | Тип | Поведение по коду |
|---|---|---|
| `sqlite_path` | `str \| None` | не задан → путь выбирает синглтон библиотеки; задан → `Path(...).expanduser()` + `mkdir(parents=True, exist_ok=True)` |
| `enabled` | `bool \| None`, дефолт `True` | выключает запись usage (graceful degradation) |

Оба ключа **опциональны**: это `_StrictOptional`, то есть неизвестный ключ
в секции приводит к отказу конфигурации, а отсутствие ключа — к дефолту.
Фактическое состояние рабочего конфига: `config.json:704-706` содержит
только `{"enabled": true}`, поэтому действует дефолтный путь
библиотеки.

Дефолты, зафиксированные в требованиях:
`<get_runtime_subdir("usage")>/usage.db` и `expanduser()` для `~`
(`Requirements`, строки 249-264) — оба выполняются.

Проверено: `get_runtime_subdir` в фабрике **не вызывается** — при
отсутствии явного пути путь выбирает синглтон. Это не расхождение
наблюдаемого поведения (итоговый путь тот же), но разница в том, кто его
выбирает, и её стоит знать при отладке пути.

## Lifecycle

У предмета нет самостоятельного жизненного цикла — он следует за
жизненным циклом `ApplicationContext` и агента.

По шагам (все ссылки на `lib/core/application_context.py`, если не
указано иное):

1. **Создание** — `_make_usage_store(ctx)` вызывается на шаге 4a, до
   сборки агента (строка 390). Вызывается **всегда**; вернуть `None` может
   сама фабрика, если конфиг выключен или библиотека недоступна
   (строки 1984-1989).
2. **Подключение** — `AgentFactory.create_agent(... usage_store=ctx.usage_store)`
   (строка 478), и обёртка уходит в `AgentLoop.from_config` как
   `provider_snapshot_loader` (`lib/core/agent_factory.py:274-277`).
   Подписка происходит **на каждой** загрузке snapshot'а, то есть
   повторно при смене модели, а не однократно на старте.
3. **Работа** — каждое наблюдение провайдера уходит в
   `usage_store.record(call)`.
4. **Закрытие** — `self.usage_store.close()` в `ApplicationContext.stop()`
   (строки 906-911); ошибка закрытия логируется предупреждением и **не**
   мешает остановке остальных сервисов (строки 909-911). Комментарий в
   коде называет закрытие «SQLite WAL».

Порядок несимметричен: создание — в `ApplicationContext`, подключение —
в `AgentFactory`, закрытие — снова в `ApplicationContext`. Ошибка на
любом из трёх шагов не мешает агенту работать, потому что учёт usage —
observability, а не бизнес-критичный путь (это сформулировано прямо в
`lib/core/agent_factory.py:306-307`).

## Data Ownership

Владелец данных — **экземпляр upstream `LLMUsageStore` и его SQLite-файл**;
агент — только создатель и держатель ссылки.

Разделение, которое спека делает нормативным и которое следует
уважать при любом изменении:

| Слой | Что пишет | Чем отличается |
|---|---|---|
| `LLMUsageStore` | usage-метаданные вызовов LLM | content-free: провайдер, модель, длительность, токены, `finish_reason` |
| `DbLoggingService` | журнал `agent_gateway_logs` | content-rich: вопросы пользователя, результаты, полезная нагрузка |

Правило: **`LLMUsageStore` не пишет содержимое вызовов, а
`DbLoggingService` не эмитит `llm_usage`.** Первое — контракт
`LLMCallRecord` (`Requirements`, строки 141-163), второе проверено
отдельным требованием «DbLoggingService не эмитит llm_usage»
(`Requirements`, строки 174-210).

Единственное место в проекте, где эти два слоя встречаются, —
`AgentFactory._wrap_provider_snapshot_loader`: в одном замыкании
подписываются и usage-стендер, и наблюдатель смены модели. Данные при
этом не смешиваются — подписчики разные, и второй получает `bus`, а не
`record`.

Кто читает данные: **никто внутри агента**. Проверено — потребителей
usage-строк в `lib/` и `workspace/` нет; это база для внешнего чтения
(BI, отчёты), и такой потребитель в репозитории не объявлен.

## Error Behavior

Три класса отказа, и все три — **fail-soft**, потому что учёт usage не
влияет на работу агента.

**1. Библиотека недоступна** (`lib/core/application_context.py:1984-1989`):
любое исключение на импорте или создании → `logger.warning("LLMUsageStore
unavailable ({}); observer will not be attached", exc)` → возврат `None`.
Поле `ctx.usage_store` остаётся `None`, подписка не происходит, агент
работает без учёта usage.

**2. Ошибка чтения конфигурации** (строки 1964-1971): `except Exception`
при `settings_section("gateway").get("usage_store", None)` → `usage_cfg =
None`. То есть битый раздел конфигурации приводит к дефолтам, а не к
падению старта.

**3. Ошибка подписки observer'а** (`lib/core/agent_factory.py:326-329`):
исключение от `set_llm_call_observer` ловится, логируется как
`"LLMUsageStore observer failed to attach: {}"` и **поглощается** —
вызов возвращает snapshot как есть. Второй подписчик (`bus`) защищён
таким же `try/except` (строки 336-339).

Специальный случай — `base_loader is None`
(`lib/core/agent_factory.py:312-314`): возвращается `None`, и upstream
использует свой дефолтный загрузчик. Это не отказ, а штатный режим
работы без учёта.

Чего **нет** и что было бы нарушением: ни один из этих классов не
поднимается наружу как отказ вызова LLM. Модель не должна видеть ошибку
подписки observer'а, потому что её действие «повторить» тут бессмысленно —
это не то, что можно починить повтором.

## Invariants

1. **Подключение происходит до первого LLM-вызова.** Обёртка передаётся в
   `AgentLoop.from_config` (`lib/core/agent_factory.py:274-277`), то есть
   раньше, чем агент получит провайдера.
2. **Подписка идёт по загрузчику snapshot'а, а не по провайдеру напрямую.**
   `AgentLoop` не отдаёт провайдер наружу
   (`lib/core/agent_factory.py:293-295`); обход этого ограничения возможен
   только через загрузчик.
3. **`set_llm_call_observer` получает `usage_store.record`, а не сам
   `usage_store`** (`lib/core/agent_factory.py:325`) — иначе в observer
   уехал бы объект целиком, а не метод записи.
4. **`LLMUsageStore` не содержит содержимого вызовов.** Поля
   `LLMCallRecord` — метаданные; добавление content нарушило бы
   content-free контракт.
5. **Один writer на слой.** `LLMUsageStore` — единственный писатель usage;
   `DbLoggingService` не эмитит `llm_usage`, и наоборот. Двойная запись
   означала бы две правды о стоимости вызовов.
6. **Схема и retention принадлежат upstream.** Ни миграции, ни
   `CREATE TABLE`, ни управления `MAX_CALLS_RETAINED` в проекте нет.
7. **Закрытие не роняет остановку** — `close()` обёрнут в `try/except` с
   предупреждением (`lib/core/application_context.py:908-911`).
8. **Отсутствие store не ломает агента.** `usage_store=None` — штатное
   состояние (выключено или библиотека недоступна), а не ошибка сборки.

## Forbidden Behavior

1. **Подключать usage к провайдеру напрямую**, минуя
   `provider_snapshot_loader`. Причина не в стиле: `AgentLoop` создаёт
   провайдер внутри `from_config(...)` и наружу его не отдаёт
   (`lib/core/agent_factory.py:293-295`), поэтому такого пути нет.
2. **Хранить содержимое вызовов** (prompt, ответ, фрагменты сообщений) в
   usage-хранилище. Это разрушает content-free контракт и превращает
   observability в копию журнала.
3. **Писать usage-события из `DbLoggingService`** или наоборот — две
   правды о стоимости вызовов.
4. **Управлять retention из проекта** (свои `MAX_CALLS_RETAINED`,
   `MAX_DAYS_RETAINED`, `VACUUM` по своему расписанию). Принадлежит
   upstream.
5. **Создавать таблицы usage руками** (миграция, `CREATE TABLE`, DDL).
6. **Обращаться к usage-таблице из агентских инструментов** — она не
   объявлена в поверхности модели и в `platform.json → audit.tables` не
   входит.
7. **Ронять старт или прерывать LLM-вызов из-за ошибки учёта usage.**
   Fail-soft обязателен: учёт — observability, а не бизнес-критичный путь
   (`lib/core/agent_factory.py:306-307`).
8. **Проглатывать ошибку подписки молча** — она обязана попадать в лог
   предупреждением; иначе пропавший учёт выглядел бы как «учёта нет».
9. **Импортировать `nanobot.llm_usage` на уровне модуля** вместо
   ленивого импорта внутри фабрики
   (`lib/core/application_context.py:1973`): ленивость здесь позволяет
   работать без storage-стека вообще.

## Consumers

Потребителей **данных** у предмета нет внутри агента — проверено поиском
по `lib/` и `workspace/`: никто не читает usage-строки. Это осознанное
решение (usage-база предназначена для внешнего чтения), а не пробел.

Потребители **подключения и состояния:**

| Потребитель | Что использует | Где |
|---|---|---|
| `AgentLoop` (nanobot) | `provider_snapshot_loader` с подпиской | `lib/core/agent_factory.py:274-277` |
| `ApplicationContext.stop()` | `usage_store.close()` | `lib/core/application_context.py:906-911` |
| `runtime_health` / startup-отчёт | счётчики подсистемы | `lib/services/runtime_health.py:134-137` |
| Контрактные тесты | сигнатуры `LLMUsageStore`/`record` | `tests/contract/test_usage_store_api.py` |
| Внешний аналитик (за пределами репозитория) | строки SQLite | вне проекта |

Модель не является потребителем: операции чтения usage у платформы нет,
и в `config.json → tools.mcpServers.enterprise.enabled_tools` такая
операция не объявлена.

## Implementation

Собственного модуля реализации у предмета **нет** — и это установлено
проверкой дерева, а не предположением. Поиск по `lib/` и `workspace/` на
`LLMUsageStore` даёт два файла, и оба — обвязка, а не хранилище.

Файлы (все пути проверены `Test-Path`, все существуют):

- `lib/core/agent_factory.py` — `_wrap_provider_snapshot_loader` (строка
  286): единственное место подписки; строки 323-329 (usage) и 330-339
  (fallback-обработчик шины).
- `lib/core/application_context.py` — `_make_usage_store` (строка 1950):
  единственное место создания; строка 390 — вызов на шаге 4a; строки
  906-911 — закрытие.
- `lib/core/project_settings.py` — `UsageStoreSettings` (строки 143-154):
  разбор секции `gateway.usage_store`.
- `./config.json` — секция `gateway.usage_store` (строки 704-706).

Внешний (не наш) код, на который спека опирается как на контракт.
Класс **не находится в репозитории** — он лежит в окружении
(`.venv/Lib/site-packages/nanobot/llm_usage/store.py`), и путь проверен
живым импортом, а не `Test-Path` по дереву:

- `nanobot.llm_usage.store` — `LLMUsageStore`, `LLMCallRecord`,
  `MAX_CALLS_RETAINED`, `MAX_DAYS_RETAINED`.

Чего в дереве репозитория **не существует** и что не следует искать:
`lib/services/usage_store.py`, `lib/storage/usage_store.py`, миграций схемы
usage, SQL с `CREATE TABLE ... usage`, инструмента чтения usage для модели,
а также копии `nanobot/llm_usage/store.py` под `lib/` — этого пути в
репозитории не существует, класс живёт только в окружении. Поскольку класс
принадлежит библиотеке, любое его изменение — это апгрейд `nanobot-ai`,
и его покрывает контракт из спеки `upgrade-compatibility`.

## Verification

Все пути проверены `Test-Path`; все существуют.

**Контрактные тесты (основной гейт):**

- `tests/contract/test_usage_store_api.py` — сигнатуры
  `LLMUsageStore.__init__` и `LLMUsageStore.record`; закрывает требования
  «LLMUsageStore конструктор принимает Path» и
  «LLMUsageStore.record принимает LLMCallRecord».
- `tests/contract/test_llm_observer_api.py` — контракт наблюдателя LLM-вызовов:
  существование `set_llm_call_observer` и пригодность его аргумента для
  `record`.
- `tests/contract/test_usage_to_dict_contract.py` — сериализация usage-строки
  в словарь (то есть форма, которую внешний потребитель читает).

**Проверки обвязки в основном наборе:**

- `tests/test_storage_hybridization_factory.py` — сборка через
  `AgentFactory`: обёртка подключается, и store остаётся опциональным;
- `tests/test_storage_hybridization_lifecycle.py` — жизненный цикл
  `ApplicationContext`, включая закрытие store;
- `tests/test_storage_hybridization.py` — общие инварианты слоёв хранения,
  в том числе single-writer;
- `tests/test_application_context_role.py` — роль `ApplicationContext` в
  создании подсистем, включая usage-store;
- `tests/test_config_keys.py` — разбор `gateway.usage_store` и допустимость
  ключей секции;
- `tests/test_runtime_health.py` — появление счётчиков в отчёте живости;
- `tests/test_docs_consistency.py` — согласованность документации.

Честная граница: **теста на content-free состав записанной строки в
нашем наборе нет** — он держится на контрактных тестах библиотеки и на
требовании «LLMCallRecord не содержит content» (`Requirements`, строки
141-163). Автоматического стража того, что проект не расширил набор
записываемых полей, репозиторий не содержит; при появлении такого
расширения инвариант 4 пришлось бы поддерживать ревью.

Отдельно: проза требований называет проверкой observer-контракта
`tests/contract/test_llm_observer_api.py` (строки 99 и 133) — файл
существует, и это единственный названный ею тест, который действительно
покрывает подключение. Имена тестов, которых в прозе нет, придуманы не
были: остальные семь путей выше перечислены как фактически существующие
стражи, найденные поиском по символам `usage_store` в `tests/`.
