# ADR-004: Граница ответственности `audit_analyzer` (Skill ↔ runtime-сервисы)

**Дата:** 2026-09-29
**Статус:** Accepted
**Контекст:** Phase 0 ревизии архитектуры `audit_analyzer`; восстановление
baseline по Git history перед любыми изменениями кода.

## Контекст

Планировался возврат `audit_analyzer` к «старой архитектуре» с тремя
операциями `run_predefined_script` / `vector_search` / `nl_sql_generate`,
которые, по первоначальной формулировке, являлись операциями самого Skill,
а не Agent Tools. Отсюда следовал план «удалить `scripts/cli.py`, не
создавая нового Tool-слоя».

Реконструкция по Git history показала, что такая постановка mixes две
разные эпохи развития подсистемы, и отменяет часть плана.

## Baseline (Phase 0)

| Capability | Вызывающая сторона (Skill-side) | Runtime владеет | Статус |
|---|---|---|---|
| predefined SQL | `predefined.run(name, db, params)` → `CacheProvider.query_sql` | DuckDB-соединение, cache lifecycle, ownership/fencing | соответствует |
| vector search | `CacheProvider.search_vector` (прямой вызов из `cli.py:448`) | FAISS, embeddings, `_index_cache` | соответствует |
| NL→SQL | `generated_sql_mode.run` → `lib.services.llm_client.call_llm` + `validate_sql` | LLM-протокол, DuckDB | соответствует |
| schema / metadata | `CacheProvider.get_schema` + skill-side `format_schema` | schema snapshot | соответствует |
| DuckDB lifecycle | — (skill не открывает) | ownership, fencing, publish path | **дефект** — skill открывает файл сам через второй провайдер |
| vector index lifecycle | — | `list_runtime_vector_indexes` | **дефект** |

Инвариант «Skill не открывает DuckDB/FAISS» **выполнен**: grep по
`workspace/skills/audit_analyzer/scripts/**.py` (без тестов) не находит
ни `import duckdb`, ни `import faiss`. Skill-side работа с кэшем идёт
через `build_cache_provider()`.

## Хронология: подсистема оциклировала минимум четыре раза

| Эпоха | Состояние | Agent-entry point |
|---|---|---|
| A | `cli.py` → `predefined_mode.run(db)` / `sql_mode.run(db)`, vector инлайн | CLI |
| B | `dd4ecdf`: + `workspace/tools/audit_analyzer_tool.py` (3 tool'а) поверх CLI | CLI **и** tools |
| C | `b62fa5d`, `c8d42df`, `6a7554e`: 4–5 tool'ов в `workspace/tools/` | tools |
| D | `fac5cf5` «полностью tool-only»: удалены **все** skill-скрипты, включая `cli.py` (−2418 строк) | только tools |
| E | `12bf182`: удалены `vector_search_tool.py` + `duckdb_query_tool.py` | остатки tools |
| F | `468a3db`: удалены `run_predefined_script.py`, `nl_sql_generate.py`, `column_descriptions.py`; NL→SQL уходит в skill-side helper | возврат CLI |
| G | `94fadf2` → HEAD: трёх-режимный `cli.py` + пакет `predefined/` + `generated_sql_mode.py` | CLI |

Состав tool'ов по датам:

| Файл | Добавлен | Удалён |
|---|---|---|
| `workspace/tools/vector_search_tool.py` | `c8d42df` | `12bf182` |
| `workspace/tools/duckdb_query_tool.py` | `d8a3d39` / `fe8c8ed` | `12bf182` |
| `workspace/tools/nl_sql_generate.py` | `b62fa5d` | `468a3db` |
| `workspace/tools/column_descriptions.py` | `b62fa5d` | `468a3db` |
| `workspace/tools/run_predefined_script.py` | `6a7554e` | `468a3db` |

`run_predefined_script` никогда не было именем Python-функции (поиск
`def run_predefined_script` по всем ревизиям — пусто): это строковый
`name` tool'а. `nl_sql_generate` существовал как tool-секция
`project.json::gateway.nl_sql_generate` (`66f5b19`), что по конвенции
проекта означает конфиг tool'а.

Ключевое: в эпохах C и E tool-слой **уже был** тонким оркестратором над
существующими сервисами. `workspace/tools/run_predefined_script.py`
прямо декларировал: «Tool **не** подключается к БД напрямую, **не**
валидирует SQL сам, **не** делает parameter substitution строкой. Только
оркестрация существующих сервисов» — `PredefinedScriptRegistry` →
`PredefinedScriptRequestBuilder` → `validate_sql` → `CacheProvider.query_sql`.

Следовательно: различие эпох — **не** в наличии лишнего слоя, а в том,
кто выполняет оркестрацию (tool или CLI). Обе эпохи соблюдают принцип
«тонкая оркестрация поверх существующих runtime-сервисов».

## Модель доступа к кэшу (принята 2026-09-29)

**Один файл кэша. Одна точка входа — интерфейс. Одна реализация.**

Runtime, skill и любой другой компонент получают доступ к файлу кэша
**только** через интерфейс `CacheProvider`. Какая реализация стоит за
интерфейсом (DuckDB, другая СУБД, файл в памяти) — **не важно и не видно
вызывающим**. Компонент, назвавший конкретный класс хранилища, нарушает
модель.

Отсюда — уточнение к настоящему ADR: skill не должен называть **и**
`DuckDbCacheStore`. Ранее (§ «Решение») предполагалось, что skill возьмёт
`DuckDbCacheStore.open(path, mode)` напрямую; это тоже нарушение, просто
менее заметное, потому что класс называет DuckDB.

### Проверено по коду: модели нет ни на одной стороне

| Ожидание | Факт |
|---|---|
| Реализация за интерфейсом одна | `class DuckDbCacheStore:` (`duckdb_cache_store.py:308`) — **не наследует** `CacheProvider`; `PostgresDuckDbProvider(CacheProvider)` (`cache_provider_impl.py:800`) — вторая реализация, живущая в слое skill'а |
| Runtime работает через интерфейс | `ApplicationContext.cache_provider: Any \| None` (`application_context.py:151`), `cache_store: Any \| None` (`:152`) — интерфейса нет и на уровне типа; `project_tool_loader.py:213` обещает tool'ам `CacheProvider`, а `:284-296` отдаёт `DuckDbCacheStore` |
| Файл один | `DuckDbCacheStore.open(path=publish_path)` → `_cache_path = path` (`:457`), затем `application_context.py:1564` присваивает `_publish_path` тот же путь → **`_cache_path == _publish_path`** |
| Снапшот — отдельный файл | `publish()` (`:920-1110`) копирует файл **сам на себя**: `ATTACH` tmp → `CREATE OR REPLACE TABLE … AS SELECT` → `close()` → `os.replace(tmp, target)` → reopen (`:1077-1102`) |

Модель «runtime держит in-memory mirror → публикует снимок для читателей»
(её описывают `preload_service.py:18-21`, `duckdb_cache_store.py:335`,
`:925-928`) **не соответствует коду**: runtime пишет в файл напрямую, а
`publish()` затем переписывает его из самого себя. Механизм temp+`os.replace`
— наследие модели «снимок», сменившейся на ownership (Stage C/D), но не
доведённой до конца.

Практическое следствие: `close()`/reopen вокруг `os.replace`
(`:1088-1102`) каждый цикл роняет и заново захватывает блокировку файла.
Именно это, а не «конфликт двух режимов», порождает недетерминированное
окно, в которое читатель то попадает, то нет.

## Решение

**Эталонной считается эпоха A/G — CLI-слой.** Обоснование — действующая
нормативная документация проекта `docs/skill-tool-architecture.md`:

- §1: Skill и Tool — независимые механизмы; Skill не вызывает Tool программно.
- §5: Skill пишет инструкции в терминах capability:
  «use `scripts/cli.py --mode vector` with `--index-name violations_index`».
- §6, §7: Agent-facing tool'ы `duckdb_query` и `vector_search` **не существуют**.
- §8: «Skill `audit_analyzer` — **CLI-only**: автономный skill-side CLI
  `scripts/cli.py --mode <predefined | generated_sql | vector>`»;
  «Tool'ы `run_predefined_script` и `nl_sql_generate` отсутствуют: их логика
  живёт в CLI skill'а (`predefined.run`, `generated_sql_mode.run`)».

`workspace/skills/audit_analyzer/scripts/cli.py` **не удаляется**: он и
является утверждённой точкой входа. Skill → существующий runtime-сервис,
без нового Tool/Service-прослойки.

## Что отменяется

- Удаление `scripts/cli.py` — противоречит `skill-tool-architecture.md` §8.
- Возврат Agent-tools для трёх режимов — противоречит §1 и §8.
- Dependency graph «Skill → Tool → Service → CacheProvider → DuckDB» —
  лишний уровень, не существовавший ни в эталонной, ни в текущей схеме.

Инвариант «Skill не открывает DuckDB/FAISS» уже выполнен и не требует
работ.

## Открытые дефекты (подтверждены кодом)

1. **`list_runtime_vector_indexes` открывает DuckDB самостоятельно.**
   `lib/services/cache_provider_impl.py:189` — `duckdb.connect(str(db_path),
   read_only=True)` в обход провайдера. При этом docstring (строка 149)
   обещает «DuckDB-коннекшен из `DuckDbCacheStore` через
   `cache_provider.PooledDuckDbConnection`» — расхождение docstring и кода.
   DI-шов **уже существует**: keyword-параметр `fetch_fn` (строка 170),
   используемый в `tools/check_indexes.py:179`. Устранение сводится к
   удалению fallback-ветки, а не к новому abstraction.

2. **Второе открытие shared-файла из отдельного процесса.**
   `cli.py::_open_db()` (строки 241-253) вызывается агентом через `tools.exec`,
   то есть в отдельном процессе, и открывает тот же DuckDB-файл, который
   удерживает gateway (`DuckDbCacheStore`, `application_context.py:1556-1559`).
   **Уточнение 2026-09-29:** ранее здесь было записано, что это «не нарушение
   архитектурной границы: `cli.py` идёт через `build_cache_provider()`, а не
   через `duckdb.connect`». Это неверно — `build_cache_provider()` возвращает
   `PostgresDuckDbProvider`, чей `_open_cache` (`cache_provider_impl.py:905`)
   и есть `duckdb.connect(str(self._cache_path), read_only=True)`. Skill
   открывает файл **в обход интерфейса**, через вторую реализацию. Именно
   поэтому дефект и есть нарушение границы, а не только process-boundary.

Оба дефекта относятся к **процессной границе**, а не к границе
Skill ↔ Tool, и чинятся независимо от настоящего ADR.

### Статус дефектов

| # | Дефект | Статус |
|---|---|---|
| 1 | `list_runtime_vector_indexes` открывает движок самостоятельно | **закрыт** — `fetch_fn` обязателен, fallback-ветки нет |
| 2 | Второе открытие shared-файла из отдельного процесса | **закрыт** change `drop-local-cache-read-from-pg` (см. ниже) |
| 3 | Расхождение трактовки `gateway.cache.local_path` | **закрыт** — `local_path` однозначно каталог, путь резолвит `resolve_cache_path()` |
| 4 | Импорт несуществующего `_WORKSPACE_ROOT`, поглощаемый как успех | **закрыт** вместе с fallback-веткой из п. 1 |
| 5 | Коллизия между changes | **закрыт** — `cache-architecture-alignment` разрешён в пользу `drop-local-cache-read-from-pg` |

**Дефект 2 закрыт полностью, а не обходным путём.** Он складывался из двух
поломок. Первая — процессная: `DuckDbCacheStore` удерживал файл всё время жизни
процесса, а skill-side CLI открывает тот же файл из отдельного процесса, то
есть «сначала закрой gateway» было единственным выходом. Теперь процесс **не
удерживает файл между операциями**: каждый read-метод открывает соединение на
время вызова и закрывает сразу после, а writer'ом является только стадия
загрузки, завершающаяся до начала чтения. Файл физически свободен всегда, и
инструкция «завершите держатель и повторите» из `SKILL.md` больше не
существует. Вторая — граница интерфейса: прежде skill получал провайдера из
`PostgresDuckDbProvider` со своим `duckdb.connect`, то есть в обход. Этой
реализации в рантайме больше нет (она осталась только в доктрингах как история);
`lib/core/skill_config.py` делегирует единственной точке создания
`open_cache_provider(mode=READ_ONLY)`, которая возвращает `DuckDbCacheStore`.
Обе поломки сняты, дефект переведён в «закрыт».

### Дополнено 2026-09-29 (после проверки существующих change)

3. **Расхождение трактовки `gateway.cache.local_path`.**
   `resolve_publish_path` (`lib/core/application_context.py:1240`)
   трактует `local_path` как **каталог** («путь к каталогу на локальной ФС,
   где будет лежать `cache.duckdb`») и дописывает имя файла.
   `list_runtime_vector_indexes` (`cache_provider_impl.py:177-186`)
   трактует его как **готовый файл** и идёт по другой ветке. Два
   потребителя одной настройки получают разные пути.

4. **Импорт несуществующего символа, проглатываемый как успех.**
   `cache_provider_impl.py:182` — `from lib.core.skill_config import _WORKSPACE_ROOT`.
   В `lib/core/skill_config.py` этого символа **нет**. `ImportError`
   поглощается `except Exception: return []` (`:190`), поэтому при
   относительном `local_path` `--list-indexes` **молча** возвращает пустой
   каталог — недоступность выдаётся за «индексов нет».

5. **Коллизия между changes.** `cache-architecture-alignment` §5.4 требовала
   удалить `PostgresDuckDbProvider`, `build_cache_provider` и
   `lib/core/skill_config.py:274-275` — то есть единственный путь, которым
   skill-side CLI получает провайдера. Вместе с §5.8 (запрет lifecycle-методов
   на ABC) это оставляло CLI **без способа получить `CacheProvider`**.
   Именно поэтому `fix-cache-process-boundary` вводил project tool — он
   заклеивал коллизию, а не решал свою задачу. Задача §5.4 исправлена
   2026-09-29: factory сохраняется и типизируется как `CacheProvider`.

6. **`DuckDbCacheStore` не реализует интерфейс, который сам же продвигает.**
   `duckdb_cache_store.py:308` — `class DuckDbCacheStore:` без базового
   класса. Реализовано 7 из 9 abstract-методов ABC; **нет `refresh`/`check_stale`**
   — поэтому класс не мог объявить наследование, не определив судьбу этих
   двух методов. Проверено: production-вызовов у них нет вообще (только сам
   `PostgresDuckDbProvider` и `tests/test_cache_provider_meta.py`), а
   репликацией PG→кэш в runtime владеет загрузчик кэша. Решение:
   оба метода уходят из ABC, репликация остаётся вне интерфейса.
   *(Уточнение 2026: владельцем репликации был `PgDuckDbSyncService`; change
   `drop-local-cache-read-from-pg` заменил его на `CacheLoadService`.)*

7. **Мёртвый instance-метод `open(self)` с ложным docstring.**
   `duckdb_cache_store.py:381-391` перекрыт `@classmethod open(cls, path, mode)`
   (`:432-459`) в теле того же класса — имя `open` принадлежит classmethod,
   тело метода недостижимо. Docstring `:385-389` называет callers
   `gateway.py` и `benchmarks/runner.py`; оба зовут `connect()`
   (`gateway.py:158`, `benchmarks/runner.py:618`). Инвариант «у каждой
   сущности один `open`» проверяется grep'ом в
   `cache-architecture-alignment` §5.9.

### Кросс-процессная конкуренция за файл кэша — решена 2026-09-29

Ранее здесь был зафиксирован открытый вопрос: сужением интерфейса
блокировку не вылечить, нужен отдельный жизненный цикл держателя. Вопрос
**закрыт решением**, а не отложен.

**Кэш объявлен process-exclusive ресурсом.** В любой момент у файла ровно
один активный владелец. Процесс, не получивший требуемый доступ при
инициализации, **не считается успешно запущенным** и завершает старт
типизированной ошибкой, называющей конфликтующий процесс.

Формулировка — свойство **ресурса**, а не способа запуска: одинаково
работает для gateway, CLI, benchmark и любого будущего отдельного процесса.
Специальных правил вида «CLI не работает при Gateway» не вводится.

**Проверкой служит сама попытка открыть файл, а не отдельный pre-check.**
Проверка занятости и последующее открытие разделены во времени:

```text
Process A                 Process B
check → свободно
                         check → свободно
open
                         open → конфликт
```

Поэтому отдельная проверка занятости запрещена как TOCTOU-гонка. Контракт
разбирает результат попытки открытия:

```text
startup → open_cache_provider() → duckdb.connect
   ├── OK           → процесс продолжает startup
   └── lock/error   → typed startup error → процесс завершается
```

**`CacheOwnershipCoordinator` сохраняется**, но роль сужается до координации
записи и синхронизации (claim, heartbeat, generation, write fence). Он не
превращается в механизм передачи открытого файла между процессами и не
используется для разрешения конкурентного доступа. Процесс, не получивший
claim, запрашивает `READ_ONLY`, сам выполняет попытку открытия и при конфликте
падает — а не ждёт освобождения.

Что отменено как избыточное: ownership transfer, закрытие gateway на время
работы CLI, межпроцессный generation fencing, временное переключение
`READ_WRITE → READ_ONLY`, автоматическое переоткрытие файла.

Нормативный контракт — `cache-architecture-alignment` §5.12-5.16; следствия
для skill'а (снятие `hasattr(open_cache)`, отказ от конструируемого
«cache not found») — `fix-cache-process-boundary` §1 и §4.

## Проверка соответствия

| Проверка | Тест |
|---|---|
| Tool не импортирует Skill | `tests/test_skill_tool_independence.py::test_tools_do_not_import_skills` |
| Skill не импортирует Tool | `tests/test_skill_tool_independence.py::test_skills_do_not_import_tools` |
| Skill не открывает DuckDB/FAISS сам | `tests/test_single_cache_interface.py` (новый, `fix-cache-process-boundary` §5.2) |
| `duckdb.connect` отсутствует в `lib/services` вне фабрики | тот же guard |
| **Ровно одна реализация `CacheProvider`** | тот же guard |
| **Никто вне слоя реализации не называет конкретный класс хранилища** | тот же guard (`fix-cache-process-boundary` §1.4) |
| **У каждой сущности один `open`** | `cache-architecture-alignment` §5.9 |

## Связанные документы

- `docs/skill-tool-architecture.md` — нормативный контракт Skill ↔ Tool.
- `docs/skill-tool-inventory.md` — текущее состояние skill/tool.
- `workspace/skills/audit_analyzer/SKILL.md` — контракт трёх режимов.
