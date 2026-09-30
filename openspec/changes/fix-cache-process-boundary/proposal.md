> ## ⚠️ Перекрыто `drop-local-cache-read-from-pg` (реализован)
>
> Наблюдаемый дефект, ради которого заведён этот change, **устранён на уровне
> причины**: процесс больше не удерживает файл кэша между операциями, поэтому
> работающий gateway не мешает skill'у, запущенному в отдельном процессе.
> Ложная ошибка «кэш занят другим процессом» и требование «завершите держатель»
> из `SKILL.md` сняты.
>
> Граница «skill не знает конкретную реализацию» из этого change **сохранена и
> закреплена гардом** `tests/test_skill_cache_boundary.py` (запрещены
> `DuckDbCacheStore` / `CacheStore`, путь к файлу кэша, прямой импорт движка,
> идентификаторы, названные по реализации). Состав этого change, предлагавший
> in-process project tool для `audit_analyzer`, **отменён отдельно**: по
> `docs/architecture/decisions/audit-analyzer-runtime-boundary.md` эталонной
> является CLI-слой навыка, agent-tools для этих режимов не возвращаются.
>
> Не начинать реализацию по текущему тексту без нового решения владельца.

## Why

**Модель (принята 2026-09-29):** существует ровно **один файл кэша**. К нему
имеют доступ **только** через **интерфейс кэша** — runtime, skill и любой
другой компонент. Какая стоит за интерфейсом реализация (DuckDB, другая
СУБД, файл в памяти) — **не важно**: компонент, назвавший конкретный класс
хранилища, нарушает модель.

**Проверено по коду: сегодня этой модели нет ни на одной из двух сторон.**

### Разрыв 1 — у runtime интерфейса нет вообще

`DuckDbCacheStore` **не наследует** `CacheProvider`:

- `lib/services/duckdb_cache_store.py:308` — `class DuckDbCacheStore:` (без
  базового класса);
- реализовано 7 из 9 abstract-методов ABC: `is_ready`, `close`, `get_schema`,
  `query_sql`, `explain`, `preload_indexes`, `search_vector`; **нет
  `refresh`/`check_stale`**;
- `lib/core/application_context.py:151-152` — `cache_provider: Any | None` и
  `cache_store: Any | None`: интерфейса нет и на уровне типа;
- `lib/services/project_tool_loader.py:213` — docstring обещает tool'ам
  `CacheProvider`, а `:284-296` передаёт им `DuckDbCacheStore`.

Итог: «интерфейс» для runtime — фикция, удерживаемая docstring'ами и
`Any`. Скилл при этом ходит в **другую** реализацию того же интерфейса —
`PostgresDuckDbProvider(CacheProvider)` (`lib/services/cache_provider_impl.py:800`),
— которая открывает тот же файл сама (`:897-905`).

Две реализации, один файл, два механизма. Это и есть «процессная граница»,
о которой этот change.

### Разрыв 2 — «снимок» и «рабочий файл» — один и тот же файл

Модель «runtime держит in-memory mirror → публикует снимок для читателей»
**не соответствует коду**:

- `lib/core/application_context.py:1556-1559` вызывает
  `DuckDbCacheStore.open(path=publish_path, mode=mode)` → `:457`
  `instance = cls(cache_path=path)`, то есть `_cache_path = publish_path`;
- `:1564` присваивает `store._publish_path = publish_path` — **тот же** путь.
  Итог: **`_cache_path == _publish_path`**, файл один;
- `publish()` (`:920-1110`) копирует этот файл **сам на себя**: `ATTACH` tmp →
  `CREATE OR REPLACE TABLE … AS SELECT` → `DETACH` → `close()` →
  `os.replace(tmp, target)` → reopen (`:1077-1102`).

Следствия, каждое из которых — наблюдаемый дефект:

1. `close()`/reopen вокруг `os.replace` каждый цикл **роняет и заново
   захватывает** блокировку файла — источник недетерминированного окна, в
   которое читатель то попадает, то нет. Комментарий `:1077-1081` описывает
   ровно этот Windows-механизм (`ERROR_SHARING_VIOLATION`), но считает его
   необходимой частью решения, а не его симптомом.
2. Docstring `:925-928` утверждает «Gateway НЕ держит его открытым» — прямо
   противоречит `:1556-1559` и собственному `:1079`.
3. `preload_service.py:18-21` описывает «in-memory mirror → snapshot file» и
   «CLI-агент остаётся чистым читателем» — обе части неверны.
4. `duckdb_cache_store.py:335` называет `_publish_path` «целевой файл снимка
   для CLI-читателей» — отдельного снимка нет.

Механизм temp+`os.replace` — **наследие модели «снимок»**, которая сменилась
на ownership (Stage C/D), но не была доведена до конца.

### Разрыв 3 — остался один настоящий обход интерфейса

`list_runtime_vector_indexes` (`cache_provider_impl.py:123-224`) открывает файл
сам, минуя и провайдера, и `resolve_publish_path()`:

- `:177-186` трактует `gateway.cache.local_path` как **файл**, тогда как
  `resolve_publish_path` (`application_context.py:1240`) трактует его как
  **каталог** и дописывает `cache.duckdb` — расхождение путей, уже
  фиксировавшееся ранее (`tools/release_v252.py:45-53`);
- `:182` — `from lib.core.skill_config import _WORKSPACE_ROOT`: символ в
  `lib/core/skill_config.py` **отсутствует**, `ImportError` глотается
  `except Exception: return []` на `:190`. Итог: при относительном
  `local_path` `--list-indexes` молча возвращает пустой каталог;
- `:190` маскирует **недоступность файла** под «каталог пуст».

### Разрыв 4 — диагностика врёт

`cache_provider_impl.py:888-895`: `open_cache()` глотает **любое** исключение
(включая конфликт блокировки) и возвращает `False`. `cli.py:245-251` на это
поднимает `FileNotFoundError("DuckDB-кеш не найден: <path>")` — блокировка
файла сообщается как «файл отсутствует» и указывает запустить
`python gateway.py`, который и вызывает блокировку.

Решение конфликта принято 2026-09-29 и отдельным change'ом
(`cache-architecture-alignment` §5.12-5.16): кэш — **process-exclusive**
ресурс, проверкой служит сама попытка открытия файла, конфликт фатален на
старте. Здесь снимается только ручное конструирование сообщения в skill'е:
после §1 и §2 кода, который превращает `False` в «файл не найден», просто
не остаётся.

### Разрыв 5 — мёртвый код с ложным docstring

`duckdb_cache_store.py:381-391` — instance-метод `open(self)`, **перекрытый**
`@classmethod open(cls, path, mode)` (`:432-459`) в теле того же класса: имя
`open` принадлежит classmethod, тело метода недостижимо. Docstring `:385-389`
называет callers `gateway.py` и `benchmarks/runner.py` — оба зовут
`connect()` (`gateway.py:158`, `benchmarks/runner.py:618`).

### Анализ callers `scripts/cli.py` (обязательный precondition по п.10)

| Caller | Тип | Факт |
|---|---|---|
| Код в `lib/`, `workspace/`, `*.py` в корне | production | **NOT FOUND** — ни один Python-модуль не вызывает `cli.py` |
| Агент через `exec` tool | production | **НАЙДЕН**: `workspace/TOOLS.md:302-316`, `workspace/skills/audit_analyzer/SKILL.md:306-309` |
| `benchmarks/runner.py`, `benchmarks/items/*` | benchmark | **NOT FOUND**; `benchmarks/items/simple.yaml:4-5` прямо утверждает, что агент CLI больше не запускает |
| `audit_analyze.bat` / `audit_analyze.sh` | legacy | **УДАЛЕНЫ** (`CHANGELOG.md:1617`, `1641-1642`); упоминания в `docs/INTERNAL_API.md:312-334` — устаревший prose |
| `tests/test_audit_analyzer_cli.py:53-59` | test | subprocess-харнесс `subprocess.run([sys.executable, CLI_PATH, ...])`; `CLI_PATH` резолвится в `tests/conftest.py:100-103` |
| `workspace/skills/audit_analyzer/tests/*` | test | in-process вызов хелперов (`test_audit_analyzer_edge_cases.py:13` — «tests call CLI helpers directly, no subprocess») |
| `tests/test_docs_consistency.py:13-16,58` | test | утверждает, что CLI — активная документированная точка входа и путь существует |

**Вывод по п.10.** Единственный легитимный caller CLI — агент через
`tools.exec`. Путь **сохраняется**: `scripts/cli.py` — утверждённая точка
входа (`docs/skill-tool-architecture.md` §8: «Skill `audit_analyzer` —
**CLI-only**»).

## What Changes

1. **Модель доступа фиксируется явно:** один файл кэша, единственная точка
   входа — интерфейс. Runtime, skill и любой другой компонент ходят в файл
   **только** через интерфейс и не называют конкретную реализацию.
2. **Точка входа не меняется.** `scripts/cli.py` сохраняется
   (`docs/skill-tool-architecture.md` §8). Доменная логика skill'а
   (predefined-реестр, validator, генерация SQL, LLM-вызов, формат вывода)
   не меняется.
3. **Skill перестаёт обходить интерфейс (4 места).** Skill-side
   `build_cache_provider()` начинает возвращать тот же `CacheProvider`,
   который использует runtime, а не отдельную реализацию
   (`fix-cache-process-boundary` §1, `cache-architecture-alignment` §5.5-5.6):
   - `cli.py:89` и `cli.py:330` — `from lib.services.cache_provider_impl import ...`
     (прямой импорт модуля реализации);
   - `cli.py:245` — `hasattr(provider, "open_cache")`: `open_cache` отсутствует
     в `CacheProvider`, это метод `PostgresDuckDbProvider`;
   - `skill_config.py:79` — `build_cache_provider() -> Any` при фактическом
     возврате конкретного класса; `skill_config.py:68` — docstring
     `get_in_memory_cache_path()` про путь DuckDB в слое skill'а.
4. **`list_runtime_vector_indexes` перестаёт открывать файл сам и резолвить
   путь сам** (`cache_provider_impl.py:177-191`). Устраняются Разрывы 3 и
   расхождение трактовки `local_path`.
5. **Ложная диагностика снимается:** недоступность файла, его отсутствие и
   «пустой результат» — различимые исходы, а не один `False`/«не найден».
6. **Мёртвый `open(self)` удаляется** (Разрыв 5), его ложный docstring — вместе
   с ним.

## Capabilities

### New Capabilities

- Нет. Capability `skills/audit-analyzer-query` **отменена**: она описывала
  project tool `audit_analyzer_query`, что противоречит принятому эталону
  (эпоха A/G, CLI-слой) и `docs/skill-tool-architecture.md` §8, а также
  заводило бы второй путь доступа к файлу вместо интерфейса.
  Каталог `specs/skills/` требует удаления вручную — локальный delete
  заблокирован политикой безопасности среды.

### Modified Capabilities

- `data/cache-provider`: интерфейс — **единственная** точка доступа к файлу
  кэша для всех компонентов; модули реализации MUST NOT открывать файл
  самостоятельно; MUST NOT существовать более одной реализации;
  возвращаемый тип фабрики — `CacheProvider`, а не конкретный класс;
  «недоступен» и «пуст» — различимые исходы.
- `architecture/skill-tool-boundary`: skill MUST NOT знать конкретную
  storage-реализацию — ни тип возврата фабрики, ни имена её методов, ни путь
  к файлу хранилища.

## Impact

**Runtime-код:**

- `lib/services/cache_provider.py` — интерфейс покрывает
  `read_vector_index_config` / `list_runtime_vector_indexes`; возвращаемый
  тип фабрик — `CacheProvider`.
- `lib/services/cache_provider_impl.py:123-224` — удаляются собственный
  `duckdb.connect` (`:189`), self-resolve пути (`:177-188`) и несуществующий
  импорт `_WORKSPACE_ROOT` (`:182`); подавление исключений уточняется.
- `lib/services/cache_provider_impl.py:366` и `lib/core/skill_config.py:274` —
  делегаты в единую точку создания провайдера с типом `CacheProvider`.
- `workspace/skills/audit_analyzer/scripts/skill_config.py:68-80` — типы и
  docstring без привязки к DuckDB.

**Skill-код (точечно, доменная логика не меняется):**

- `workspace/skills/audit_analyzer/scripts/cli.py:89`, `:245`, `:330` —
  снятие обходов интерфейса.

**Тесты:**

- `tests/test_audit_analyzer_cli.py` — контракт сохраняется, subprocess-харнесс
  остаётся (точка входа не меняется).
- Новый guard `tests/test_single_cache_interface.py` — **ровно одна**
  реализация `CacheProvider` в репозитории; ни один модуль под
  `workspace/skills/` не содержит `cache_provider_impl`, `duckdb.connect`,
  `DuckDbCacheStore(`, `PostgresDuckDbProvider(`, `open_cache`,
  `gateway.cache.local_path`; ни один модуль вне слоя реализации не называет
  конкретный класс хранилища.

**Документация:** `docs/skill-tool-architecture.md` §2,
`SKILL.md` § «Runtime boundary», ADR
`docs/architecture/decisions/audit-analyzer-runtime-boundary.md`,
`CHANGELOG.md`.

## Out of Scope

- **Сам контракт владения файлом кэша.** Конфликт за файл между процессами
  больше не открытый вопрос, а решённое правило: кэш process-exclusive,
  проверкой служит сама попытка открытия, второй процесс не запускается с
  типизированной ошибкой. Реализуется в `cache-architecture-alignment`
  §5.12-5.16. Здесь снимается только следствие для skill'а — ручное
  конструирование «cache not found» вместо разбора реальной причины.
- **Наследование `DuckDbCacheStore` от `CacheProvider`, судьба
  `refresh`/`check_stale`, удаление `PostgresDuckDbProvider`,
  удаление `publish()`-как-self-replace, типизация
  `ApplicationContext.cache_provider`, роль `CacheOwnershipCoordinator`** —
  `cache-architecture-alignment` §5 (ядро реализации; этот change фиксирует
  модель и границу skill'а).
- ownership, heartbeat, fencing, shutdown ordering, producer lifecycle,
  удаление alias `cache_store`, типизированные storage-ошибки —
  `cache-architecture-alignment`.
- `role`, `ApplicationContext`, CLI/Gateway client architecture, Cron —
  `unify-runtime-channels`.
- Создание `workspace/tools/audit_analyzer_query.py` и удаление
  `scripts/cli.py` — **отменено**.
- IPC-абстракция не вводится.
- Бизнес-логика `audit_analyzer` — сохраняется как есть.
