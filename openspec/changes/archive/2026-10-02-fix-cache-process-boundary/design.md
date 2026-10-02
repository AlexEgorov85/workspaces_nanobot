## Context

Принятая модель доступа (2026-09-29): **один файл кэша, одна точка входа —
интерфейс, одна реализация.** Runtime, skill и любой другой компонент
обращаются к файлу **только** через `CacheProvider`. Какая реализация стоит
за интерфейсом (DuckDB, другая СУБД, файл в памяти) — не важно и не видно
вызывающему.

`proposal.md` фиксирует пять разрывов между этой моделью и кодом. Здесь —
решение, как skill получает те же данные, не обходя интерфейс, и почему
именно так.

Определяющее наблюдение: **интерфейса для runtime не существует вовсе.**
`DuckDbCacheStore` (`duckdb_cache_store.py:308`) объявлен как
`class DuckDbCacheStore:` — без наследования `CacheProvider`;
`ApplicationContext.cache_provider` типизирован `Any | None`
(`application_context.py:151-152`); DI-шов `project_tool_loader.py:284-296`
отдаёт tool'ам `DuckDbCacheStore`, тогда как docstring `:213` обещает
`CacheProvider`. При этом `PostgresDuckDbProvider(CacheProvider)`
(`cache_provider_impl.py:800`) — единственный реальный наследник, и живёт он
в слое skill'а, открывая тот же файл сам (`:905`).

Итог: **две реализации, один файл, два механизма.** Это и есть разрыв, который
чинит change.

## Goals / Non-Goals

**Goals:**

- Skill получает доступ к кэшу через тот же интерфейс и ту же точку создания
  провайдера, что и runtime, — не через отдельную реализацию.
- Ни skill, ни runtime не называют конкретный класс хранилища.
- Ни один модуль runtime'а не открывает файл кэша в обход интерфейса.
- Диагностика не может сказать «кеш не найден» про ошибку блокировки.
- Бизнес-логика skill'а сохраняется; меняется только способ доставки данных.

**Non-Goals:**

- Ядро интерфейса: наследование `DuckDbCacheStore`, судьба
  `refresh`/`check_stale`, удаление `PostgresDuckDbProvider`, удаление
  `publish()`-как-self-replace, типизация `ctx.cache_provider` (change
  `cache-architecture-alignment` §5).
- Ownership, heartbeat, fencing, shutdown ordering (тот же change).
- `role`, `ApplicationContext`, Cron, CLI/Gateway client architecture
  (change `unify-runtime-channels`).
- IPC любого вида.

## Decisions

### Decision 1: Skill ходит в интерфейс, а не в реализацию

**Что:** skill-side `build_cache_provider()` начинает возвращать тот же
`CacheProvider`, который использует runtime, с той же точки создания
(`cache-architecture-alignment` §5.4). Сегодня он возвращает
`PostgresDuckDbProvider` — вторую реализацию интерфейса, существующую только
для skill'а.

**Почему не «пусть skill открывает `DuckDbCacheStore` напрямую»:** это тоже
нарушение модели, просто менее заметное, потому что класс называет DuckDB.
Критерий — не «не упоминать DuckDB», а «не называть конкретный класс
хранилища». Иначе первая же смена реализации (DuckDB → что-то другое) снова
потребует правок в skill'е.

**Почему не отдельный transport/IPC для skill'а:** IPC — отдельный
транспортный слой с отдельным жизненным циклом, обработкой ошибок и
сериализацией. Он заведёт **второй** путь доступа к тому же файлу, то есть
ровно то нарушение, которое change устраняет.

### Decision 2: `scripts/cli.py` сохраняется

**Что:** точка входа skill'а не меняется. Обоснование — нормативный контракт
`docs/skill-tool-architecture.md` §8: «Skill `audit_analyzer` — **CLI-only**»,
плюс §5 (инструкции в терминах capability `scripts/cli.py --mode vector`),
§6/§7 (agent-facing tool'ы `duckdb_query` и `vector_search` не существуют).

**Что меняется в `cli.py`:** только способ получения провайдера и снятие
протечек реализации. Доменная логика (`predefined/`, `generated_sql_mode.py`,
`llm.py`, `output.py`, JSON-контракт stdout, error-EXIT contract) не трогается
и не переносится.

**Почему не отдельная standalone-обёртка «на всякий случай»:** обёртка,
которая открывает файл кэша, — это ровно тот второй процесс, который
контракт запрещает. Пока она существует и задокументирована, она будет
вызвана снова: `workspace/TOOLS.md:302-316` и `SKILL.md:306-309` сегодня
именно так её и вызывают.

### Decision 3: `list_runtime_vector_indexes` работает через провайдера

**Что:** `list_runtime_vector_indexes()` (`cache_provider_impl.py:123-224`)
перестаёт создавать собственное соединение:

```text
было:  list_runtime_vector_indexes(store_table, fetch_fn=None)
        └── fetch_fn is None → САМ резолвит путь + duckdb.connect

стало: данные читаются через уже полученный от интерфейса provider
```

Параметр `fetch_fn` сохраняется как injection-шов для низкоуровневого
использования, но функция MUST NOT разрешать `gateway.cache.local_path`
самостоятельно и MUST NOT импортировать `lib.core.skill_config`.

**Что это чинит помимо основной задачи:**

- Расхождение трактовки `local_path` (файл в `:177-186` против каталога в
  `resolve_publish_path`).
- Несуществующий импорт `_WORKSPACE_ROOT` (`:182`), из-за которого
  относительный `local_path` давал `ImportError` → `return []` (поглощение на
  `:190`) — молчаливый пустой каталог индексов.
- Второе открытие файла в том же subprocess, что и основной путь: даже если
  убрать только основной open, `--list-indexes` продолжил бы открывать файл.

### Decision 4: «cache not found» перестаёт быть конструируемым сообщением

**Что:** в skill-коде **нет ни одного** вызова, открывающего файл кэша, и
нет кода, превращающего неудачный open в
`FileNotFoundError("DuckDB-кеш не найден")` — он исчезает вместе с
`hasattr(provider, "open_cache")` (`cli.py:245`), который к тому же ссылался на
метод, которого нет в интерфейсе.

**Где живёт настоящая типизация ошибок:** storage-уровень
(`UnsupportedFilesystemError` и типизированные open-ошибки) — change
`cache-architecture-alignment` §7. Здесь оно намеренно **не дублируется**,
чтобы у одного класса ошибок не было двух владельцев.

**Проверка, которая закрывает путь:** guard-тест
`tests/test_single_cache_interface.py` — ни одного совпадения по
`duckdb.connect` / `open_cache` / `DuckDbCacheStore(` / `PostgresDuckDbProvider(`
в skill-пакете. Это дешёвый тест без запуска DuckDB.

### Decision 5: мёртвый код и ложные docstring'и убираются вместе с моделью

**Что:** модель «снимок» сменилась на ownership, но не была доведена до конца,
и оставила за собой код, который врёт:

- `open(self)` (`:381-391`) перекрыт `@classmethod open` (`:432-459`) в теле
  того же класса — тело недостижимо, а docstring называет callers, которые
  зовут `connect()` (`gateway.py:158`, `benchmarks/runner.py:618`).
- Docstring `publish()` (`:925-928`) «Gateway НЕ держит его открытым»
  противоречит `:1556-1559` и собственному `:1079`.
- `preload_service.py:18-21` описывает «in-memory mirror → snapshot file»,
  которого нет: `_cache_path == _publish_path`.

Docstring, утверждающий модель владения файлом, которая не соответствует
коду, — дефект того же класса, что и сам дефект: он уводит следующего
разработчика.

## Risks / Trade-offs

**[Risk]** Skill лишается возможности работать, когда файл держит другой
процесс. → Это не регрессия, а точная формулировка проблемы: сейчас skill
получает `False` из `open_cache()` и сообщает «файл не найден». Новый
контракт (решение 2026-09-29, `cache-architecture-alignment` §5.12-5.16)
делает конфликт явным и фатальным на старте, вместо того чтобы маскировать
его под «кеш не найден».

**[Risk]** Удаление `PostgresDuckDbProvider` ломает вызовы, которые тот
обслуживал. → Проверено: единственные production-вызовы его методов —
`refresh()`/`check_stale()` внутри него самого, у которых вызывающих нет;
`load_cache_from_postgres` и `check_cache_stale` используются только тестами.
Реальный доступ skill'а идёт через `open_cache`/`_open_cache`, которые
заменяются общей точкой создания.

**[Risk]** `tests/test_audit_analyzer_cli.py` (в т.ч. subprocess-харнесс на
`:53-59`) падает. → Точка входа не меняется, поэтому харнесс остаётся
рабочим; правятся только тесты, фиксировавшие конкретный класс реализации.

**[Risk]** Конфликт по файлам с change `cache-architecture-alignment`: оба
трогают `lib/services/cache_provider_impl.py`. → Разграничение зафиксировано
в Scope Boundary: этот change убирает **обход интерфейса со стороны skill'а**;
`cache-architecture-alignment` перерабатывает **storage-слой и lifecycle**.

## Scope Boundary

> Этот change устраняет обход интерфейса при доступе к кэшу. Он не является
> redesign ownership или cache lifecycle.

Явно **не** входит:

| Тема | Владелец |
|---|---|
| наследование `DuckDbCacheStore`, судьба `refresh`/`check_stale`, единая точка создания | `cache-architecture-alignment` §5 |
| удаление `PostgresDuckDbProvider`, удаление `publish()`-как-self-replace | `cache-architecture-alignment` §5 |
| ownership, `try_claim`, generation, heartbeat, write fence, shutdown ordering | `cache-architecture-alignment` |
| `UnsupportedFilesystemError` и типизированные open-ошибки, FS prober | `cache-architecture-alignment` §7 |
| `enable_audit` и cache lifecycle, `worker_id`, alias `cache_store` | `cache-architecture-alignment` |
| `role`, `ApplicationContext`, Cron, CLI client, typed events | `unify-runtime-channels` |
| конкурентное чтение из второго процесса | `cache-architecture-alignment` §5.12-5.16 (process-exclusive, конфликт фатален на старте) |
| IPC | никто (в этом цикле не требуется) |

## Migration Plan

1. **Phase 1.** Единая точка создания провайдера в слое интерфейса;
   `DuckDbCacheStore` объявляет наследование; `ctx.cache_provider`
   типизируется `CacheProvider | None` (change `cache-architecture-alignment`).
2. **Phase 2.** Skill-side `build_cache_provider` начинает возвращать тот же
   `CacheProvider`; `PostgresDuckDbProvider` удаляется.
3. **Phase 3.** `list_runtime_vector_indexes` работает через провайдера;
   удаляются `duckdb.connect` (`:189`) и импорт `_WORKSPACE_ROOT` (`:182`).
4. **Phase 4.** Снятие `hasattr(provider, "open_cache")` и импортов
   `cache_provider_impl` в `cli.py`; guard-тест
   `tests/test_single_cache_interface.py`.
5. **Phase 5.** Удаление мёртвого `open(self)` и правка ложных docstring'ов.
6. **Phase 6.** Обновление `SKILL.md`, `TOOLS.md`,
   `docs/skill-tool-inventory.md`, ADR; архивация change.

**Rollback:** фазы 3–5 обратимы независимо (правки локальны и не меняют
публичные контракты). Фазы 1–2 обратимы одним revert'ом: удаление второй
реализации — единственное изменение, затрагивающее оба процесса сразу.

## Open Questions

- ~~Конкурентное чтение~~ — **решено 2026-09-29.** Кэш объявлен
  process-exclusive ресурсом: один активный владелец, второй процесс не
  запускается. Проверкой служит сама попытка открытия файла, отдельный
  pre-check занятости запрещён (TOCTOU-гонка: оба процесса увидят «свободно»
  и оба упадут). `CacheOwnershipCoordinator` сохраняется для координации
  записи/синхронизации и не используется для передачи открытого файла.
  Нормативный контракт — `cache-architecture-alignment` §5.12-5.16; следствия
  для этого change (снятие `hasattr(open_cache)`, отказ от конструируемого
  «cache not found») — §1 и §4.
- `tests/test_audit_analyzer_cli.py` сохраняет имя файла или переименовывается.
  Решается при реализации; на контракт не влияет.
- `legal_summarizer` (`workspace/tools/legal_summarizer_query.py:256-283`)
  запускает свой CLI в subprocess, но работает с DOCX/PDF, а не с кэшем.
  Проверено: файл кэша он не открывает. Если появится cache-зависимость —
  применяется Decision 1 этого change.

---

## Дополнение 2026-10-02: почему дельты спеки не применяются

Change закрыт по коду (32/32). Дельты не применяются, и на это две разные
причины — важно их не путать.

### 1. `skills/audit-analyzer-query` — дельта к несуществующей capability

Единственное содержимое дельты — `## REMOVED Requirements` с требованием
«capability `skills/audit-analyzer-query` не вводится», плюс раздел
«Обоснование отмены». Целевой спеки
`openspec/specs/skills/audit-analyzer-query/spec.md` не существует и не
должна: отмена capability ничего не удаляет из несуществующего файла. Дельта
самопротиворечива — отменяет то, чего нет.

### 2. `data/cache-provider` — дельта противоречит текущей архитектуре

Здесь ситуация не в пользу архивации, и это находка.

**Активная спека `openspec/specs/data/cache-provider/spec.md` описывает
подсистему, которой больше нет.** Она содержит 41 упоминание
`CacheProvider` и требования про `CacheOwnershipCoordinator`,
`query_sql` с `READ_ONLY`/`READ_WRITE`, `CacheAccessMode`,
`gateway.cache.local_path` — всё это снесено фазами 1.4, 5 и 9 миграции
`enterprise-mcp-platform`.

Дельта этого change'а в тот же capability добавляет два требования, которых
в активной спеке нет, и переписывает «единый интерфейс доступа» в сторону
`CacheProvider` как **единственного** пути доступа. Применение дельты:

- **вернуло бы в норму интерфейс, которого в агенте нет**, — то есть откатило
  бы сделанное;
- оставило бы саму активную спеку в том же устаревшем состоянии, потому что
  дельта касается трёх требований из тридцати.

Переписывать `data/cache-provider` здесь нельзя: capability описывает снимок,
а его новое место — capability `data` платформы, и это зона change'а
`enterprise-mcp-platform`, который выполняется параллельно. Две правки одной
спеки из двух писателей дали бы расхождение, а не исправление.

**Поэтому:** дельты сняты, в `.openspec.yaml` выставлен `skip_specs: true`.

**Что осталось за change'ом `enterprise-mcp-platform`:** переписать
`openspec/specs/data/cache-provider/spec.md` под фактическое состояние —
снимок принадлежит capability `data` платформы, `CacheProvider` в агенте
нет, владение файлом обеспечивается тем, что файл не удерживается между
операциями, а writer один и известен заранее. Инварианны, которые защищали
этот change, живы и проверяются: `duckdb.connect` запрещён везде, кроме
владельца файла (`mcp-platform/tests/test_architecture_boundaries.py:82`),
навык не владеет данными
(`tests/test_skill_tool_independence.py`,
`mcp-platform/tests/test_audit_tables_ownership.py`), имена таблиц не зашиты
(`tests/test_no_hardcoded_table_names.py`).
