# Канон `data/cache-provider` говорит неправду: снятый кэш-кластер агента описан как живой

## Why

`openspec/specs/data/cache-provider/spec.md` описывает мир, в котором агент владел
локальным DuckDB-кэшем: слой владения с fencing'ом, фоновый инкрементальный sync,
`resolve_publish_path(role)` с двумя role-based путями, `gateway.cache.local_path`,
`CacheSyncService`, `CacheProvider` как поле `ApplicationContext`. **Этого кода в
репозитории нет уже давно** — он уехал в capability `data` платформы
(`mcp-platform/libs/enterprise_data/snapshot/`), и `openspec/specs/OWNERSHIP.md:27`
это уже признаёт, указывая преемника `DuckDbSnapshotStore`.

Дыра старая, и это важно для оценки срочности: её не создала сегодняшняя работа.

**Почему её никто не закрыл.** Оба change'а, снимавшие кэш-кластер, архивированы
**без каталога `specs/`**, то есть нормативный текст они не тронули:

- `openspec/changes/archive/2026-10-02-drop-local-cache-read-from-pg/` — только
  `.openspec.yaml`, `design.md`, `proposal.md`, `tasks.md`;
- `openspec/changes/archive/2026-10-02-fix-cache-process-boundary/` — тот же набор.

Дельты у них не было, а значит `openspec archive` нечего было мержить в канон. Код
при этом изменился радикально. Итог: канон `data/cache-provider` остался
единственным местом, где снятая подсистема описана как работающая, и любая
сверка «спека против кода» по ней даёт ложь.

**Почему это становится срочным именно сейчас.** Канон `storage/session-hybridization`
уже приведён к коду (change
`2026-10-04-session-hybridization-canon-gap`), и `runtime/entrypoints` уже
зафиксировал в таблице «Снятые требования»
(`openspec/specs/runtime/entrypoints/spec.md:815-820`), чем именно заменены
кэш-требования. То есть **один канон говорит «этого нет», второй говорит
«это есть»**. После пересборки `unify-runtime-channels` (его дельта трогает
`runtime/entrypoints`) это станет прямое противоречие между двумя канонами
внутри одного репозитория.

## Разбор канона по требованиям

Сверка по сырым байтам: `lib/`, `mcp-platform/libs/`, `mcp-platform/servers/`,
`mcp-platform/platform.json`, `config.json`, `profiles/`, `workspace/`, `tools/`.

| # | Требование канона | Что в коде | Расхождение | Решение | Код-основание |
|---|---|---|---|---|---|
| 1 | `PostgreSQL — источник истины` | `SnapshotLoadService` читает PG, отдаёт снимку строки; таблица заменяется целиком | `PgDuckDbSyncService` не существует; инкрементального sync по track-колонке нет | `MODIFIED` | `PgDuckDbSyncService` — **0 совпадений в коде**, два упоминания в
докстрингах `lib/services/db_logging_service.py:209`, `:1504`;
`loader.py:149-157`, `:321` |
| 2 | `локальный ext4 storage` | `resolve_snapshot_path(cache_dir, filename)`; путь в `platform.json → data.snapshot_path`; NFS-отвергнение есть | `gateway.cache.local_path` нет в `config.json`; `resolve_publish_path` 0 совпадений; `role` 0; слой владения 0 | `MODIFIED` | `config.json` `"cache"`/`local_path` — **0 совпадений**; `profiles/test.jsonc` — **0 совпадений**; `store.py:258-270`, `:87`, `platform.json:95` |
| 3 | `единый интерфейс доступа` | Навык зовёт операции платформы (`mcp_enterprise_*`); `CacheProvider` — ABC платформы | Навык не может вызвать `query_sql()`: объекта нет в его процессе | `MODIFIED` | `config.json` `enterprise_mcp` (объявлен); `contracts.py:195`; `workspace/skills/audit_analyzer/SKILL.md` |
| 4 | `vector search только через CacheProvider.search_vector` | FAISS принадлежит `libs/vectors`; `search_vector` делегирует `VectorIndexAccessor` | FAISS не может быть у владельца снимка; `index_path` не портирован намеренно | `MODIFIED` | `store.py:9-17`, `:273-295`, `:786-811`, `:798-801` |
| 5 | `контроль целостности индексов` | `verify_index_signature()` → `IndexIntegrityError` `STALE`/`INVALID` до эмбеддинга | **Совпадает.** Поведение то же, владелец другой | `MODIFIED` (только владелец) | `libs/vectors/owner.py:344-351`; `signature.py:12` |
| 6 | `Storage implementation isolation` | `import duckdb` только в модуле снимка; FAISS только в `libs/vectors` | Из перечня потребителей мертвы `CacheSyncService`, `CacheOwnershipCoordinator` | `MODIFIED` | `store.py:1`, `:298`; `CacheSyncService` — **0 совпадений** |
| 7 | `CacheProvider как интерфейс без конкретной СУБД` | `CacheProvider(ABC)` без `open()`; фабрика — модульная функция | Пункт «factory вызывает `ApplicationContext`» неверен: это composition root платформы | `MODIFIED` | `contracts.py:195,223-280`; `store.py:1408`; `application_context.py:761` |
| 8 | `CacheOwnershipCoordinator как абстрагированный ownership` | **Ничего.** Класс, таблица, все методы отсутствуют | Требование целиком описывает несуществующий компонент | `REMOVED` | `CacheOwnershipCoordinator`, `try_claim`, `acquire_write_fence`,
`ClaimResult`, `agent_cache_ownership` — **0 совпадений в коде**; имя класса
осталось в прозе (`snapshot/contracts.py:56`, `docs/TARGET-ARCHITECTURE.md:51`) |
| 9 | `query_sql mode semantics` | `assert_query_allowed()`: DDL → `UnsupportedSqlError` в любом mode; DML при `read_only` → `ReadOnlyAssertionError`; SELECT проходит | **Совпадает дословно.** Не трогаем | оставить как есть | `sql_guard.py:101-120`; `contracts.py:105,122` |
| 10 | `CacheAccessMode и двухуровневая защита` | `read_only=True` в `connect()` + assertion-guard `_assert_writable` | Двухуровневая защита **совпадает**. Ложна только строка выбора режима: `ClaimResult.acquired` → `READ_WRITE` | `MODIFIED` | `store.py:334`, `:427`, `:575-591`, `:1002-1020`; `contracts.py:46-58`; `ClaimResult` — **0 совпадений** |

### Два класса расхождения — смешаны не были

**Прошлый мир, кода нет** → `MODIFIED` / `REMOVED` (требования 1, 2, 3, 4, 6, 7, 8,
10). Здесь канон не молчит, а говорит неправду: называет класс, функцию, таблицу и
настройку, которых не существует.

**Настоящее и всё ещё нужное, чего в каноне нет** → `ADDED` (4 требования). Это не
ложь, а молчание, и лечится добавлением:

1. **Владелец векторных индексов отделён от владельца снимка** — FAISS запрещён в
   модуле снимка, индекс строит владелец на старте сервера, делегирование через
   `VectorIndexAccessor` (`store.py:9-17`, `:273-295`).
2. **Файл не удерживается между операциями, провайдер не бывает полуготовым** —
   соединение на время вызова; неудача открытия всегда исключение
   (`store.py:22-24`, `:380`, `:444`, `:1444-1453`;
   `mcp-platform/tests/test_snapshot_no_file_hold.py`).
3. **Единственный писатель — стадия загрузки** — `replace_records` из
   runtime-пути, `upsert_records` остаётся примитивом, роли чтения и записи
   разведены на уровне ABC (`store.py:25-29`; `loader.py:149-157`, `:321`;
   `contracts.py:195,285,358`).
4. **Ненастроенный/непригодный снимок отказывает операциям, а не роняет сервер** —
   `UnavailableSnapshot` с кодом причины (`server.py:484-504`;
   `unavailable.py:28-32`; `platform.json:95` + `data._about`;
   `mcp-platform/tests/test_snapshot_optional_startup.py`).

### Найдено сверх названного

- **`refresh()` и `check_stale()` не существуют.** `## Public Contract` канона
  (строки 58-59) перечисляет их как часть API. Поиск `def refresh` / `def check_stale`
  по `mcp-platform/libs/enterprise_data/snapshot/*.py` — **0 совпадений**. В ABC их
  нет тоже (`contracts.py:223-280` — 7 абстрактных методов, `refresh`/`check_stale`
  не среди них).
- **Все 7 файлов из `## Dependencies` / `## Implementation` отсутствуют:**
  `lib/services/cache_provider.py`, `cache_provider_impl.py`, `duckdb_cache_store.py`,
  `pg_duckdb_sync_service.py`, `vector_index_service.py`, `table_registry.py`,
  `lib/core/infra_registration.py` — **все MISSING**. Плюс `tools/build_vectors.py`
  (MISSING) и два теста из `## Verification`
  (`tests/test_duckdb_cache_store.py`, `tests/test_pg_duckdb_sync_service.py` —
  MISSING). Навык `legal_summarizer`, которого канон касается в тексте
  (`workspace/skills/legal_summarizer/SKILL.md`), — MISSING.
- **`gateway.enable_audit` в `config.json` — 0 совпадений.** Поле
  `ApplicationContext.enable_audit` осталось (`application_context.py:114`, `:186`),
  но к снимку отношения не имеет. Канон строит на нём требование
  «cache lifecycle MUST быть отделён от `gateway.enable_audit`» — предпосылки нет.
- **Ссылка на чужой канон устарела:** «См. подробный контракт в
  `runtime/entrypoints`» (строка 99) отсылает к разделу, который сегодня говорит
  обратное — что claim'а нет (`entrypoints:815-816`).
- **`search_vector` без `index_path`:** канон (строка 61) требует
  `search_vector(query, index_name, index_path, top_k, threshold)`. Параметр
  удалён намеренно — persist-файлов индекса в платформе нет, иначе появился бы
  второй способ указать, где взять индекс (`store.py:798-801`).

## Кому принадлежит починка

`openspec/specs/OWNERSHIP.md:20-29`: `data/cache-provider` помечена `platform`, то
есть предмет реализован в `mcp-platform`, и преемник назван явно —
`mcp-platform/libs/enterprise_data/snapshot/store.py` (`DuckDbSnapshotStore`).
Возможность не меняется, поэтому `## Scope` в дельте остаётся `` `platform` `` и
`OWNERSHIP.md` этот change не трогает.

**Это отдельный change, а не часть `unify-runtime-channels`, по трём причинам.**

1. Разные предметы. `unify-runtime-channels` трогает каналы и
   `runtime/{entrypoints,context,cli-client}` — его дельта физически не содержит
   `data/cache-provider`. Смешивать значит тащить в чужой архив правку спеки, к
   которой он отношения не имеет.
2. Разные поверхности. Здесь не меняется ни строчка кода: исправляется только
   нормативный текст. `unify-runtime-channels` меняет runtime-поведение.
3. Решение владельца R2 уже принято именно так: правка канона зеркала ушла в
   `2026-10-04-session-hybridization-canon-gap`, а не в `unify-runtime-channels`.
   Этот change — её парный для второго из двух канонов, оставшихся неправдой.

## What Changes

Дельта по `data/cache-provider`
(`openspec/changes/2026-10-04-close-cache-provider-canon-gap/specs/data/cache-provider/spec.md`):

- `## REMOVED Requirements` — одно требование, `CacheOwnershipCoordinator как
  абстрагированный ownership`: компонента нет, заменять нечем;
- `## MODIFIED Requirements` — семь требований (1, 2, 3, 4, 5, 6, 7, 10), все
  нормативные посылки переписаны по коду;
- `## ADDED Requirements` — четыре требования о том, что реально есть, но не
  описано;
- требование «`query_sql mode semantics» (9) **не тронуто**: оно совпадает с кодом
  дословно, и `MODIFIED` означало бы переписать верное ради новой формулировки.

**Заголовки сценариев сохранены все.** `MODIFIED`-блок содержит каждый
`#### Scenario:` затронутого требования, включая те, чьи условия стали
невозможны: такие тела перевёрнуты или помечены, но заголовок не выброшен.
Архив отказывается выбрасывать сценарий из `MODIFIED`-блока
(`omits scenario(s) the current spec still has`) — это уже случалось в этом
проекте. Единственное требование, чьи сценарии сняты, — `REMOVED`, и там это
объявлено явно, с перечислением.

### Capabilities

#### New Capabilities

- Нет. Новых компонентов не создаётся.

#### Modified Capabilities

- `data/cache-provider`: одно требование снято, семь исправлено, четыре добавлены.
  Новая capability не заявляется, поэтому `openspec/specs/OWNERSHIP.md` не
  меняется.

## Известные смежные расхождения (не в объёме этого change)

Найдено при сверке, **не правится здесь** — у каждого своя поверхность и своя
правка. Перечислены, чтобы не потерялись.

- **Три упоминания снятых классов остались в прозе кода.** `PgDuckDbSyncService` —
  дважды в докстрингах агента (`lib/services/db_logging_service.py:209`, `:1504`),
  при этом самого класса нет; `CacheOwnershipCoordinator` — в докстринге
  `mcp-platform/libs/enterprise_data/snapshot/contracts.py:56` и в
  `mcp-platform/docs/TARGET-ARCHITECTURE.md:51`. Кода там нет, но тот, кто ищет
  «кто такой `try_claim`», находит ссылку и не находит определения. Висячие
  ссылки на подсистему, снятую 2026-10-02, — то есть ровно тот класс расхождения,
  который этот change и закрывает, но уже в другом файле. Дельта их не трогает:
  это правка кода, три строки текста. **Нужен отдельный change.**
- **Три упоминания снятых классов остались в прозе кода.** `PgDuckDbSyncService` —
  дважды в докстрингах агента (`lib/services/db_logging_service.py:209`, `:1504`),
  при этом самого класса нет; `CacheOwnershipCoordinator` — в докстринге
  `mcp-platform/libs/enterprise_data/snapshot/contracts.py:56` и в
  `mcp-platform/docs/TARGET-ARCHITECTURE.md:51`. Кода там нет, но тот, кто ищет
  «кто такой `try_claim`», находит ссылку и не находит определения. Висячие
  ссылки на подсистему, снятую 2026-10-02, — то есть ровно тот класс расхождения,
  который этот change и закрывает, но уже в другом файле. Дельта их не трогает:
  это правка кода, три строки текста. **Нужен отдельный change.**
- **NFS-детектор работает только на Linux.** `reject_unsupported_filesystem()`
  завершается no-op на Windows и macOS (`store.py:150`, `:160-161`), и это
  закреплено тестом `::TestNoFileHold::test_non_linux_is_a_noop`. То есть
  требование «network/shared filesystem MUST быть отвергнуты» на этой машине **не
  выполняется**. Требование в каноне оставлено как есть: ослаблять его под
  фактический пробел — значит зафиксировать в каноне ошибку. **Нужен отдельный
  change платформы** (владелец — `mcp-platform`), потому что это правка кода, а
  не нормативного текста.
- **Проза вне `## Requirements` останется ложной после архивации.** Механизм
  дельт OpenSpec мержит только требования. Секции `## Public Contract`
  (`refresh()`/`check_stale()`, `index_path`), `## Dependencies`,
  `## Implementation`, `## Consumers`, `## Lifecycle`, `## Configuration`,
  `## Error Behavior` перечисляют несуществующие файлы и методы, и ни одна дельта
  их не затрагивает. См. следующий раздел.
- **`COMPONENTS.md` всё ещё перечисляет `CacheProvider` и `VectorIndexService` как
  компоненты агента** и указывает файлы, которых нет. Это уже зафиксировано в
  `OWNERSHIP.md:92-95`; файл не `openspec/**`, здесь не правится.
- **Сообщение `reject_unsupported_filesystem` советует настроить
  `gateway.cache.local_path`** (`store.py:193`) — настройки, которая больше не
  существует. Косметика, но оператору вводит в заблуждение. Правка кода
  платформы, мелкая.

## Что дельта не может закрыть

Честно фиксирую границу инструмента, а не свою недоработку.

`openspec archive` применяет к канону **требования**. Прозуические разделы
(`## Purpose`, `## Scope`, `## Public Contract`, `## State`, `## Dependencies`,
`## Configuration`, `## Lifecycle`, `## Error Behavior`, `## Invariants`,
`## Forbidden Behavior`, `## Consumers`, `## Implementation`, `## Verification`)
через дельту не проходят. Для `data/cache-provider` это значит, что после
архивации этого change в каноне останутся правдивыми требования и останутся
ложными:

- `## Public Contract`: `refresh()`, `check_stale()`, `search_vector(…,
  index_path, …)`, «ABC в `lib/services/cache_provider.py`»;
- `## Dependencies` / `## Implementation`: семь отсутствующих файлов плюс
  `tools/build_vectors.py`;
- `## Lifecycle`: `PgDuckDbSyncService` как стадия 3, `CacheProvider.refresh()`
  как стадия 2;
- `## State`: «DuckDB-файл кэша по пути `gateway.cache.local_path`»;
- `## Error Behavior`: «**Config missing**: fail fast при старте
  (`ConfigurationError`)» — прямо противоречит коду: пустой
  `ENTERPRISE_SNAPSHOT_PATH` даёт поднявшийся сервер и `UnavailableSnapshot`
  (`server.py:484-488`). Это единственное **прямое противоречие** прозы с кодом,
  и `ADDED`-требование «Ненастроенный или непригодный снимок отказывает
  операциям, а не роняет сервер» фиксирует правую сторону, но старую строку в
  `## Error Behavior` не отменяет;
- `## Verification`: ссылки на два отсутствующих теста.

**Нужен отдельный change**, который перепишет прозу целиком. Вариантов два, и
выбор не мой: (а) расширить формат дельт до разделов, (б) править эти разделы
канона напрямую, приняв, что канон меняется не только через архив. Оба затрагивают
`architecture/component-model`, то есть не `data/cache-provider`, и потому этот
change их не делает.

## Граница с кодом

**Чем я не занимался и почему.** Правки кода в этом change нет, и это не
упущение, а граница объявления: «твой закрывает только ложь в нормативном
тексте», владение — только новый каталог change, `openspec/specs/**` и код вне
границы.

- Ни один файл в `lib/`, `mcp-platform/`, `config.json`, `platform.json` не
  изменён.
- NFS-пробел (§ «Известные смежные расхождения») **не закрыт** и не обойдён
  формулировкой: требование оставлено в каноне в исходном виде, а расхождение
  объявлено.
- Никаких `git add` / `git commit` не выполнялось.
- Правка прозы канона (`## Error Behavior` и остальные разделы) **не выполнена** —
  дельта её не покрывает, см. § «Что дельта не может закрыть».

## Impact

- **Код:** не меняется.
- **Спецификация:** `data/cache-provider` в
  `openspec/changes/2026-10-04-close-cache-provider-canon-gap/specs/data/cache-provider/spec.md`.
  Capability не добавляется и не удаляется, `OWNERSHIP.md` не меняется. Дельта в
  канон **не применяется** — только через `openspec archive`.
- **Тесты:** не меняются и не запускаются; ни одно требование дельты не завязано на
  несуществующий тест (все упомянутые тесты платформы существуют).
- **Проверка:** `openspec validate 2026-10-04-close-cache-provider-canon-gap` —
  зелёный; `python tools/validate_component_specs.py --strict` — без роста ошибок
  относительно baseline (3 ошибки, все в `runtime/entrypoints`, вне этого change).
