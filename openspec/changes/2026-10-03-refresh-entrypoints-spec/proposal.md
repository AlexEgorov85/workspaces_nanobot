# Точки входа: спека приведена в соответствие с кодом

## Why

Спека `runtime/entrypoints` описывала несуществующую систему. Замер по
`git HEAD`: **909 строк, 24 требования, 99 упоминаний снятых сущностей, из них
32 — как живые зависимости.** Кэш-кластер (снимок, загрузчик, реестр ресурсов,
слой владения) удалён фазой 5 и `enterprise-mcp-platform`; в агенте не осталось
ни файла, ни поля, ни функции, которые спека требовала.

**Проверка кода, а не `AGENTS.md`.** `AGENTS.md` описывает удаления, но сам
может отставать, поэтому каждое утверждение перепроверено чтением исходников.
Расхождение нашлось и в другую сторону: часть «снятого» переехала на платформу
и **жива** — `CacheProvider`, `CacheAccessMode`, `ReadOnlyAssertionError`,
`UnsupportedSqlError`, `CacheStore` существуют в
`mcp-platform/libs/enterprise_data/snapshot/contracts.py`. Такие имена в спеке
остались, но только как **владельцы на стороне платформы**, и больше не
обязывают агента ни к чему. Проверено также, что `lib/services/duckdb_cache_store.py`,
`cache_ownership.py`, `cache_provider.py`, `pg_duckdb_sync_service.py`,
`cache_load_service.py`, `cache_sync_service.py`, `table_registry.py` отсутствуют
и на диске, и в `lib/core/application_context.py` (остались только комментарии
о снятии).

Удаление устаревшей части было вынесено в находку №1 изменения
`2026-10-02-startup-dependency-contract` — с прямым указанием, что это отдельная
работа. Настоящее изменение её выполняет.

### Что именно врёт

| Утверждение спеки (было) | Факт в коде |
|---|---|
| `ApplicationContext` создаёт `PostgresChannel` (таблица composition, сценарий `role="gateway"`) | `ApplicationContext` его не создаёт вообще. Канал создаёт `ChannelFactory.create_all()` внутри `gateway._run` (`gateway.py:413-420`), и только если `channels.postgres.enabled=True` и задан DSN (`channel_factory.py:110-121`) |
| `RuntimeEventsSubscriber` — строка composition «✅/✅» без указания места | создаётся в `start()`, а не в `create()` (`application_context.py:637-650`) |
| `WebSocket port availability` MUST быть вызван **ДО** `ApplicationContext.start()` | вызывается **после** `ctx.start()`, перед `GatewayRunner.run_forever` (`gateway.py:160-169`) |
| `gateway MUST exit 1 до подъёма runtime` при занятом порте | `SystemExit(1)` происходит уже после `ctx.start()` (`gateway.py:164,622`) — «до подъёма runtime» неверно |
| порт WebSocket — `127.0.0.1:8765` как константа | читается из `ctx.config.channels.websocket`, дефолты применяются только при отсутствии конфигурации (`gateway.py:598-604`) |
| `/compact`: `compact(session_key=..., idle=True, force=True)` | `idle` вычисляется из текста команды (`/compact idle\|--idle\|-i`), по умолчанию `False`; `force=True` всегда (`console_loop.py:141-145`) |
| `/compact`: «событие `context_compacted` MUST быть записано в `agent_gateway_logs`» | имя события — `agent.compacted`; и на CLI-пути оно **не пишется вообще**: `ContextCompactionService` конструируется без `db_logging_service` (`console_loop.py:143`), `try_log_event(None, …)` возвращает `False` с WARNING (`db_logging_service.py:79-84`). Заметка в `agent_conversation_messages` тоже не пишется: `_write_history_notice` выходит рано для не-`postgres` префикса (`context_compaction.py:526-530`) |
| `InboundMessage(channel="cli", chat_id="cli:<session>")` | `chat_id` — имя сессии из `--session` либо `"direct"`; префикс `cli:` добавляется к ключу сессии, а не к `chat_id` (`console_loop.py:241-243,395-403`) |
| `Gateway MAY принимать --profile` | `MAY` — слово неверно: профиль **обязателен** (`gateway.py:74-76`) и ограничен whitelist'ом `("prod","test")` |
| «`test` в CLI = тот же runtime **плюс CacheProvider interface**» | кэш-провайдера в агенте нет; фраза удалена |
| `DbLoggingService` — «если `gateway.enable_db_logging=True`» | одного флага мало: нужны `logging.db.enabled=True` и DSN (`application_context.py:1286-1294`) |
| `enable_audit` управляет sync-веткой и др. | поле осталось (`ctx.enable_audit`), но **больше нигде не читается** — grep по `lib/` и entrypoint'ам даёт только присваивание. Мёртвый флаг |
| «Удаление `streamlit_app.py` и `SubprocessManager.spawn_streamlit` — отдельный change `remove-streamlit-runtime`» | такого change в `openspec/changes/` нет, а сами файлы уже удалены. Требование поставлено перед несуществующей задачей |
| `enable_db_logging`/`print_llm_calls` в таблице composition без оговорок | `print_llm_calls` идёт в CLI-вывод, `enable_db_logging` — гейт над `logging.db` |

## What Changes

`openspec/specs/runtime/entrypoints/spec.md` переписан: **909 → 718 строк,
24 → 19 требований.** Кодировка и переводы строк сохранены (UTF-8 без BOM, LF).

1. **Снято 5 требований фазы 5 целиком**, каждое — с указанием замены (таблица в
   новом § «Снятые требования»): claim → mode → open; atomic claim + advisory-lock
   fencing; layered `CacheProvider` API; `CacheSyncService`; строки кэша в таблице
   composition. Замены проверены в коде, а не названы по имени: интерфейс и режимы
   — `mcp-platform/libs/enterprise_data/snapshot/contracts.py`; загрузка —
   `loader.py::SnapshotLoadService`; состав таблиц и индексов —
   `mcp-platform/platform.json`.
2. **`## Purpose` переписан**: кэш-кластер убран из описания, добавлено
   предложение о том, что спекой снятая подсистема не описывается.
3. **Таблица composition переписана по факту**: добавлена колонка «где создаётся»,
   исправлен владелец `PostgresChannel`, отмечено, что `RuntimeEventsSubscriber`
   создаётся в `start()`, и выписаны реальные дополнительные условия
   (`logging.db.enabled`, DSN, `channels.postgres.enabled`). Дублировавшаяся
   абзацная формулировка про `role` (в исходнике она повторялась дважды, второй
   раз уже после снятия кэша) схлопнута в одну.
4. **Требования, оставленные в силе, исправлены по коду** (11 пунктов в таблице
   выше). Ни одно требование не удалено «за ненадобностью» — каждое либо
   подтверждено строкой исходника, либо переписано.
5. **Добавлены сценарии, закрывающие обнаруженные расхождения**: чтение
   WebSocket-порта из конфигурации; whitelist профиля gateway; одинаковый порядок
   старта в ветвях `--patched` и обычной; allowlist-семантика deprecated kwargs
   (`TypeError` на опечатку).
6. **`/compact` переписан как граница, а не как обещание.** Прежний сценарий
   требовал записи в журнал, которой на CLI-пути нет и быть не может без
   передачи `db_logging_service`. Теперь зафиксировано фактическое поведение
   обоих путей: CLI — без записи (с объяснением, почему), tool
   `compact_context` — с записью.

## Проверка

- **`_check_refs.py` (одноразовый инструмент, удалён по завершении)** проверяет
  обе формы ссылок: `путь:строка` (файл есть, диапазон в границах) и
  `путь::символ` (файл есть, символ реально определён). Результат: **38 ссылок,
  0 битых** (exit 0). Инструмент проверен на себе: подстановка несуществующего
  символа даёт exit 1, то есть проверка не пустая.
- **Ссылки на файлы под параллельной правкой — в форме `путь::символ`, а не
  `путь:строка`.** 5 из 9 файлов, на которые ссылается спека, в момент работы
  правит другой воркер (`gateway.py`, `lib/core/application_context.py`,
  `lib/services/channel_factory.py`, `lib/services/context_compaction.py`,
  `lib/services/db_logging_service.py` — все `M` в `git status`). Номер строки
  там протухает за часы: `application_context.py` вырос на ~20 строк прямо во
  время работы, и номера, выверенные в начале, стали неверны. Имя символа не
  протухает, поэтому для этих файлов выбран второй вид ссылки. Для чистых файлов
  (`cli_agent.py`, `lib/cli/console_loop.py`, `lib/lifecycle/gateway_runner.py`,
  `workspace/tools/compact_context.py`) оставлены точные `путь:строка`.
  Первая версия спеки содержала 8 битых ссылок (`application_context.py:386` без
  каталога и т. п.) — поймал `_check_refs.py`, не глазами.
- **`_count_stale.py` (одноразовый инструмент, удалён по завершении)** режет
  спеку на нормативную часть и § «Снятые требования» и считает упоминания
  снятых сущностей **по абзацам**: абзац с упоминанием проходит, только если в
  нём самом есть маркер снятия или запрета. Так «`DuckDbCacheStore.open(...)`
  MUST …» считается мусором, а «файлы уже удалены, на диске их нет» — фактом.
  Единица анализа — абзац, а не строка: русская проза переносится, и «сущность
  удалена» может стоять в следующей строке того же абзаца. Результат:
  **было 32 живые ссылки → стало 0** (exit 0 против 1). Проверка не пустая: на
  baseline она падает и перечисляет 32 места.
- **`tests/test_gateway_enterprise_mcp_startup.py` — 58 passed.** На них
  ссылаются все требования контракта запуска; каждый упомянутый тест существует
  (сверка списка классов/методов с текстом спеки). Промежуточно прогон
  показывал 5 падений, пока другой воркер правил `gateway.py`; повторный прогон
  после окончания его правки — 58 passed. Правка спеки на тесты не влияет: она
  документационная.
- Кодировка: BOM отсутствует, CRLF — 0, все переводы строк — LF, файл
  заканчивается переводом строки (как и был).
- Символы, на которые ссылается спека, выверены точечным чтением:
  `ApplicationContext.create`, `::_resolve_enable_kwargs`,
  `::DEPRECATED_ENABLE_KWARGS`, `::_make_cron_service`, `::_make_db_logging`,
  `ChannelFactory._add_postgres`, `gateway.py::_parse_args`,
  `::_entrypoint_main`, `::_run`, `::_check_websocket_port_available`,
  `ContextCompactionService._record_event_log`,
  `::ContextCompactionService._write_history_notice`, `try_log_event`,
  плюс `gateway_runner.py` (backoff-сообщение) и `session_storage.py`
  (выбор режима хранилища).

## Находки, требующие решения заказчику

Правкой **не** выполняются — это находки о коде или о нехватке требований.
1. **`enable_audit` — мёртвый флаг.** Присваивается в
   `application_context.py:252`, далее не читается: grep по `lib/`, `gateway.py`,
   `cli_agent.py` даёт только присваивание и упоминания в тестах. При этом он
   остаётся в `DEPRECATED_ENABLE_KWARGS`, в `config.json`-дефолтах и в спеке.
   Спека теперь описывает его как часть compatibility boundary (фактически), но
   **нужно ли его снимать** — отдельный change.
2. **Сверка профильных имён таблиц на CLI — граница, а не паритет.** Спека
   фиксирует, что CLI сверку делает (это подтверждено кодом и тестами), но
   сводку по capability — нет. Это осознанная граница, и она теперь записана
   явно. Вопрос прежний: расширять ли сводку на CLI.
3. **Пробел в требованиях: кто проверяет, что записи в `agent_gateway_logs`
   не теряются на CLI-пути.** Сейчас `try_log_event` при `svc is None` пишет
   WARNING и возвращает `False`, то есть потеря события **тиха для вызывающего**.
   Для tool-пути это нормировано (write-only producer), для CLI-ручки сжатия —
   непреднамеренная потеря наблюдаемости. Нормативного требования на этот счёт
   нет; писать его без решения заказчика я не стал.
4. **Ни одно требование этой спеки не проверяется автоматически** (кроме
   контракта запуска, на который есть тесты). Требования о composition,
   ролях, профиле и флагах живут без стража. Известный долг, не закрытый здесь.

## Порядок работ (обязателен)

1. Принять объём: 5 снятых требований, 19 требований в файле, 717 строк.
2. Решить по находкам 1–4.
3. Прогнать страж: `python -m pytest tests/test_gateway_enterprise_mcp_startup.py -q`
   — ожидается `58 passed`.
4. Заархивировать change по общим правилам.

## Capabilities

### New Capabilities
- Нет. Новых capability не создаётся: контракт точек входа — часть существующей
  `runtime/entrypoints`.

### Modified Capabilities
- `runtime/entrypoints` (spec-driven): **5 требований снято** (кэш-кластер: claim →
  mode → open, atomic claim + advisory-lock fencing, layered `CacheProvider` API,
  `CacheSyncService`, строки кэша в composition), **19 требований остаётся**, из
  них 11 исправлено по коду (владелец `PostgresChannel`, момент WebSocket-проверки,
  чтение порта из конфигурации, `idle` в `/compact`, имя события `agent.compacted`
  и граница записи в журнал, форма `chat_id`, обязательность `--profile` и его
  whitelist, реальные условия создания `DbLoggingService`, мёртвый `enable_audit`,
  несуществующий change `remove-streamlit-runtime`). Добавлен § «Снятые
  требования» — таблица снятого с указанием замены и места, где замена живёт.

## Impact

- **Спецификация:** `openspec/specs/runtime/entrypoints/spec.md` — переписан,
  909 → 718 строк, 24 → 19 требований. UTF-8 без BOM, LF, завершающий перевод
  строки сохранены. Ссылки на места — в форме `путь:строка` для чистых файлов и
  `путь::символ` для файлов под параллельной правкой.
- **Change-запись:** `openspec/changes/2026-10-03-refresh-entrypoints-spec/`
  (этот файл + `.openspec.yaml`), по структуре соседних in-flight changes.
  Регистрация в `COMPONENTS.md` / `OWNERSHIP.md` не требуется: in-flight changes
  не добавляют записи до архивации.
- **Код, тесты, DDL, данные:** не затрагиваются. Правка строго документационная.
- **Одноразовые инструменты:** `_count_stale.py` и `_check_refs.py` в корне
  репозитория — созданы для машинной проверки этой правки и удалены по
  завершении работы.
- **Чужая территория не затронута:** `openspec/specs/observability/logging-db/**`,
  `openspec/changes/2026-10-02-journal-observability/**`,
  `openspec/changes/2026-10-02-startup-dependency-contract/**`, `lib/`,
  `mcp-platform/`, `tests/` — не изменялись.
- **Открытые вопросы к заказчику:** четыре находки выше.
