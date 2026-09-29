## 0. Модель доступа (принята 2026-09-29)

**Один файл кэша. Одна точка входа — интерфейс. Одна реализация.**

Runtime, skill и любой другой компонент получают доступ к файлу кэша
**только** через интерфейс `CacheProvider`. Какая реализация стоит за
интерфейсом (DuckDB, другая СУБД, файл в памяти) — **не важно и не видно
вызывающим**. Компонент, назвавший конкретный класс хранилища, нарушает
модель.

Следствие, которое раньше формулировалось неверно: **skill не должен
называть и `DuckDbCacheStore`**. Skill ходит в тот же интерфейс, что и
runtime, — через ту же точку создания провайдера.

Никаких новых Tool-слоёв. `scripts/cli.py` **сохраняется**: он и есть
утверждённая точка входа (`docs/skill-tool-architecture.md` §8). Доменная
логика skill'а (predefined-реестр, validator, генерация SQL, LLM-вызов,
формат вывода) **не меняется**.

Правило: прежде чем вводить новый слой — найти существующий интерфейс,
которому ответственность уже принадлежит. Если интерфейс не покрывает
потребность — **расширить существующий интерфейс**, а не создавать новый.

**Разделение с `cache-architecture-alignment` §5.** Там — ядро: наследование
`DuckDbCacheStore` от `CacheProvider`, судьба `refresh`/`check_stale`,
удаление второй реализации `PostgresDuckDbProvider`, удаление
`publish()`-как-self-replace, единая точка создания провайдера. Здесь —
**граница skill'а**: чтобы скилл ходил в интерфейс, а не в реализацию.
`fix-cache-process-boundary` опирается на §5 и не дублирует его.

## 1. Skill получает провайдера через интерфейс, а не через реализацию

- [ ] 1.1 Skill-side `build_cache_provider()` возвращает тот же `CacheProvider`,
      что и runtime, а не отдельную реализацию. Сегодня это
      `PostgresDuckDbProvider` (`cache_provider_impl.py:800`) — вторая
      реализация интерфейса, живущая в слое skill'а; она и удаляется
      (`cache-architecture-alignment` §5.5). verify: skill-side вызов
      разрешается в ту же реализацию, что и `ctx.cache_provider`
- [ ] 1.2 `lib/core/skill_config.py:274` `build_cache_provider(...) -> Any` →
      `-> CacheProvider`; verify через `typing.get_type_hints`
- [ ] 1.3 `workspace/skills/audit_analyzer/scripts/skill_config.py:79`
      `build_cache_provider() -> Any` → `-> CacheProvider`; verify импорт
      `CacheProvider` из `lib.services.cache_provider`
- [ ] 1.4 **Ни skill, ни runtime не называют конкретный класс хранилища.**
      Точка создания провайдера — одна функция в слое интерфейса
      (`cache-architecture-alignment` §5.4); вызывающий код её результат
      использует как `CacheProvider`. verify: `grep -rn "DuckDbCacheStore\|PostgresDuckDbProvider"
      lib/ workspace/ tools/ benchmarks/ gateway.py cli_agent.py` — хит только
      в модулях реализации и в тестах самой реализации
- [ ] 1.5 Путь к файлу кэша skill не вычисляет: `get_in_memory_cache_path()`
      (`skill_config.py:68`) используется только для сообщения об ошибке, сам
      путь хранения в слое skill'а не нужен. verify grep по skill-коду на
      `local_path`/`cache.duckdb` даёт 0 hit в вычислительном коде
- [ ] 1.6 Новых lifecycle-методов на ABC **не добавлять**
      (`cache-architecture-alignment` §5.11): открытие существующего файла
      на чтение — через существующую точку создания с `mode=READ_ONLY`

## 2. `list_runtime_vector_indexes` перестаёт открывать файл

Подтверждённые дефекты: собственный `duckdb.connect`, расхождение трактовки
`local_path`, несуществующий импорт.

- [ ] 2.1 Удалить `conn = duckdb.connect(str(db_path), read_only=True)`
      (`cache_provider_impl.py:189`); функция MUST работать только через
      переданный provider/connection, verify `grep -n "duckdb.connect"
      lib/services/cache_provider_impl.py` не показывает хит внутри функции
- [ ] 2.2 Удалить self-resolve пути (`:177-186`): `local_path` там трактуется
      как **файл**, тогда как `resolve_publish_path` трактует его как
      **каталог**; verify отсутствие собственного `Path(local_path)`
- [ ] 2.3 Удалить `from lib.core.skill_config import _WORKSPACE_ROOT` (`:182`) —
      символ в `lib/core/skill_config.py` **НЕ СУЩЕСТВУЕТ**; ImportError
      проглатывался `except Exception: return []` (`:190`), из-за чего
      относительный `local_path` молча давал пустой каталог, verify
      `python -c "from lib.core.skill_config import _WORKSPACE_ROOT"` падает
      и в тесте зафиксировано, что относительный путь больше не даёт `[]`
- [ ] 2.4 Убрать безлогичное подавление исключений: недоступность файла —
      либо `[]` с warning, либо типизированная ошибка; исключение MUST NOT
      маскировать «каталог пуст» и «каталог недоступен», verify тест на
      каждый исход
- [ ] 2.5 Docstring (`:128-153`) привести в соответствие с кодом: сейчас
      обещает «DuckDB-коннекшен из `DuckDbCacheStore` через
      `cache_provider.PooledDuckDbConnection`», чего в коде нет
- [ ] 2.6 Сохранить `fetch_fn` как injection-шов; caller обязан передать
      соединение; обновить `tools/check_indexes.py:179-180`; verify
      `python tools/check_indexes.py --json` работает против фикстуры

## 3. Skill перестаёт импортировать модуль реализации

Четыре места, где skill знает про конкретную storage-реализацию.

- [ ] 3.1 `cli.py:89` `from lib.services.cache_provider_impl import read_vector_index_config`
      → брать из интерфейсного/конфиг-слоя; verify grep по
      `workspace/skills/audit_analyzer/**` на `cache_provider_impl` даёт 0 hit
- [ ] 3.2 `cli.py:330` `list_runtime_vector_indexes` → вызывать через
      провайдера, а не импортом из impl-модуля
- [ ] 3.3 `cli.py:245` `if hasattr(provider, "open_cache")` — убрать `hasattr`:
      duck-typing по методу, которого нет в интерфейсе, и есть признак
      протечки. Ошибка MUST NOT конструироваться вручную из булева
      результата open, verify тест: сбой открытия не маскируется под
      «файл не найден»
- [ ] 3.4 `cli.py:242` docstring «Открыть DuckDB-кэш через CacheProvider» →
      формулировка от реализации («DuckDB-кэш») к интерфейсу
- [ ] 3.5 `skill_config.py:68-76` `get_in_memory_cache_path()` — docstring
      «Путь к DuckDB-кешу skill'а» убрать; функция используется для
      сообщения об ошибке, сам путь в слое skill'а не нужен, verify
      оставшийся вызов работает и не раскрывает путь хранения

## 4. Мёртвый код и ложные docstring'и, порождённые старой моделью

- [ ] 4.1 `duckdb_cache_store.py:381-391` instance-метод `open(self)`
      **перекрыт** `@classmethod open(cls, path, mode)` (`:432-459`) в теле
      того же класса и потому недостижим; удалить вместе с ложным docstring,
      называющим callers `gateway.py` и `benchmarks/runner.py` (оба зовут
      `connect()`). verify `grep -n "def open" lib/services/duckdb_cache_store.py`
      даёт ровно одно определение
- [ ] 4.2 `duckdb_cache_store.py:925-928` «Gateway НЕ держит его открытым» —
      ложь, противоречит `:1556-1559` и собственному `:1079`. Правка — в
      `cache-architecture-alignment` §5.8 вместе с удалением
      `publish()`-как-self-replace; здесь — зафиксировать в guard'е, что
      docstring не должен утверждать модель владения файлом
- [ ] 4.3 `preload_service.py:18-21` «in-memory mirror → snapshot file» и
      «CLI-агент остаётся чистым читателем» — обе части неверны
      (`_cache_path == _publish_path`); docstring приводится в соответствие
      после §5.8

## 5. Тесты

- [ ] 5.1 `tests/test_skill_tool_independence.py` (существующий) остаётся
      зелёным; verify
- [ ] 5.2 Новый guard `tests/test_single_cache_interface.py`: **ровно одна**
      реализация `CacheProvider` в репозитории (никаких вторых подклассов);
      ни один модуль под `workspace/skills/` не содержит `cache_provider_impl`,
      `duckdb.connect`, `DuckDbCacheStore(`, `PostgresDuckDbProvider(`,
      `open_cache`, прямого чтения `gateway.cache.local_path`; verify guard
      проходит и падает на заведомо добавленном нарушении
- [ ] 5.3 Регресс на расхождение путей: `gateway.cache.local_path` как
      директория разрешается одинаково в `resolve_publish_path` и в потребителях
- [ ] 5.4 Регресс на относительный `local_path` (бывший silent `[]`)
- [ ] 5.5 Регресс: недоступный файл отличается от пустого результата
- [ ] 5.6 `python -m pytest tests/ workspace/skills/audit_analyzer/tests -q` —
      без новых падений относительно baseline

## 6. Документация и проверки

- [ ] 6.1 `docs/architecture/decisions/audit-analyzer-runtime-boundary.md` —
      сверить с фактическим состоянием после выполнения
- [ ] 6.2 `workspace/skills/audit_analyzer/SKILL.md` § «Runtime boundary» —
      убрать формулировки, привязывающие skill к DuckDB
- [ ] 6.3 `docs/skill-tool-architecture.md` §2 — пример импорта
      `from lib.services.cache_provider_impl import build_cache_provider`
      заменить на интерфейсный вызов
- [ ] 6.4 `python tools/architecture_guard.py` — без новых нарушений
- [ ] 6.5 `python tools/diagnose_startup.py` — OK
- [ ] 6.6 Запись в `CHANGELOG.md` `[Unreleased]`

## Вне scope этого change

- **Кросс-процессная блокировка одного файла.** Один файл — один держатель:
  процесс с ownership (`application_context.py:1552`) держит файл в
  `READ_WRITE`, и второй процесс не может открыть его даже с
  `read_only=True`. Ограничение движка на уровне файла: subprocess не
  разделяет соединение с gateway, поэтому сужением интерфейса оно **не
  лечится**. Этот change убирает параллельные реализации, ложную
  диагностику и мёртвый код, но не конкурентное чтение — см.
  `cache-architecture-alignment` §5.12, где это зафиксировано как открытый
  вопрос.
- Ядро интерфейса (наследование `DuckDbCacheStore`, судьба
  `refresh`/`check_stale`, удаление `PostgresDuckDbProvider` и
  `publish()`-как-self-replace, типизация `ctx.cache_provider`) —
  `cache-architecture-alignment` §5.
- ownership, heartbeat, fencing, producer lifecycle, типизированные
  storage-ошибки, удаление alias `cache_store` — `cache-architecture-alignment`.
- Создание `workspace/tools/audit_analyzer_query.py` и удаление
  `scripts/cli.py` — **отменено**: противоречит принятому эталону
  (эпоха A/G, CLI-слой) и `docs/skill-tool-architecture.md` §8, а также
  завело бы второй путь доступа к файлу вместо интерфейса.
