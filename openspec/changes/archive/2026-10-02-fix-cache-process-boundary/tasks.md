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

- [x] 1.1 Skill-side `build_cache_provider()` возвращает тот же `CacheProvider`,
      что и runtime, а не отдельную реализацию. Сегодня это
      `PostgresDuckDbProvider` (`cache_provider_impl.py:800`) — вторая
      реализация интерфейса, живущая в слое skill'а; она и удаляется
      (`cache-architecture-alignment` §5.5). verify: skill-side вызов
      разрешается в ту же реализацию, что и `ctx.cache_provider`
- [x] 1.2 `lib/core/skill_config.py:274` `build_cache_provider(...) -> Any` →
      `-> CacheProvider`; verify через `typing.get_type_hints`
- [x] 1.3 `workspace/skills/audit_analyzer/scripts/skill_config.py:79`
      `build_cache_provider() -> Any` → `-> CacheProvider`; verify импорт
      `CacheProvider` из `lib.services.cache_provider`
- [x] 1.4 **Ни skill, ни runtime не называют конкретный класс хранилища.**
      Точка создания провайдера — одна функция в слое интерфейса
      (`cache-architecture-alignment` §5.4); вызывающий код её результат
      использует как `CacheProvider`. verify: `grep -rn "DuckDbCacheStore\|PostgresDuckDbProvider"
      lib/ workspace/ tools/ benchmarks/ gateway.py cli_agent.py` — хит только
      в модулях реализации и в тестах самой реализации
- [x] 1.5 Путь к файлу кэша skill не вычисляет: `get_in_memory_cache_path()`
      (`skill_config.py:68`) используется только для сообщения об ошибке, сам
      путь хранения в слое skill'а не нужен. verify grep по skill-коду на
      `local_path`/`cache.duckdb` даёт 0 hit в вычислительном коде
- [x] 1.6 Новых lifecycle-методов на ABC **не добавлять**
      (`cache-architecture-alignment` §5.11): открытие существующего файла
      на чтение — через существующую точку создания с `mode=READ_ONLY`

## 2. `list_runtime_vector_indexes` перестаёт открывать файл

Подтверждённые дефекты: собственный `duckdb.connect`, расхождение трактовки
`local_path`, несуществующий импорт.

- [x] 2.1 Удалить `conn = duckdb.connect(str(db_path), read_only=True)`
      (`cache_provider_impl.py:189`); функция MUST работать только через
      переданный provider/connection, verify `grep -n "duckdb.connect"
      lib/services/cache_provider_impl.py` не показывает хит внутри функции
- [x] 2.2 Удалить self-resolve пути (`:177-186`): `local_path` там трактуется
      как **файл**, тогда как `resolve_publish_path` трактует его как
      **каталог**; verify отсутствие собственного `Path(local_path)`
- [x] 2.3 Удалить `from lib.core.skill_config import _WORKSPACE_ROOT` (`:182`) —
      символ в `lib/core/skill_config.py` **НЕ СУЩЕСТВУЕТ**; ImportError
      проглатывался `except Exception: return []` (`:190`), из-за чего
      относительный `local_path` молча давал пустой каталог, verify
      `python -c "from lib.core.skill_config import _WORKSPACE_ROOT"` падает
      и в тесте зафиксировано, что относительный путь больше не даёт `[]`
- [x] 2.4 Убрать безлогичное подавление исключений: недоступность файла —
      либо `[]` с warning, либо типизированная ошибка; исключение MUST NOT
      маскировать «каталог пуст» и «каталог недоступен», verify тест на
      каждый исход
- [x] 2.5 Docstring (`:128-153`) привести в соответствие с кодом: сейчас
      обещает «DuckDB-коннекшен из `DuckDbCacheStore` через
      `cache_provider.PooledDuckDbConnection`», чего в коде нет
- [x] 2.6 ~~Сохранить `fetch_fn` как injection-шов~~ → **переформулировано при
      реализации.** Исходная формулировка требовала передавать *соединение*,
      но у `CacheProvider` нет способа отдать соединение наружу — поэтому
      либо соединение leaks наружу, либо (как сделано) чтение идёт через
      `query_sql` провайдера. Итог: параметр переименован в `provider=`,
      `list_runtime_vector_indexes(provider=...)` читает через интерфейс,
      `tools/check_indexes.py` открывает провайдера сам (`mode=READ_ONLY`)
      и инициализирует `SETTINGS`; `python tools/check_indexes.py` даёт
      exit 0. Открытая часть: три call-site'а обязаны передавать
      `provider=` — это было закрыто guard'ом
      `TestDiscoveryRequiresProvider::test_every_call_site_passes_provider`
      (пункт 5.2).
      **ИТОГ 2026-10-02:** guard и сам `cache_provider_impl` снесены фазами 5
      и 9, функция живёт как `mcp-platform/libs/vectors/runtime.py`. Требование
      перенесено дословно и теперь защищено стражем платформы
      `test_architecture_boundaries.py:82` — `duckdb.connect` запрещён везде,
      кроме владельца файла снимка. Регрессия 2.1/2.2 закрыта тестом
      `mcp-platform/tests/test_vectors_signature.py:264`.
## 3. Skill перестаёт импортировать модуль реализации

Четыре места, где skill знал про конкретную storage-реализацию.

- [x] 3.1 `cli.py:89` `from lib.services.cache_provider_impl import read_vector_index_config`
      → брать из интерфейсного/конфиг-слоя; verify grep по
      `workspace/skills/audit_analyzer/**` на `cache_provider_impl` даёт 0 hit.
      **ПРОВЕРЕНО 2026-10-02:** grep даёт 0 hit. Сам `cli.py` удалён фазой 9
      `enterprise-mcp-platform`, импортировать больше нечего. Навык перестал
      владеть данными; чтение конфигурации индексов живёт в
      `mcp-platform/libs/vectors/config.py:69`. Прежняя пометка
      «skill по-прежнему импортирует конфиг-хелперы» снята как неактуальная.
- [x] 3.2 `cli.py:330` `list_runtime_vector_indexes` → вызывать через
      провайдера, а не импортом из impl-модуля
      **ЧАСТИЧНО:** провайдер передаётся (`provider=db`), собственного
      соединения нет; остаётся перенести саму функцию из `impl`-модуля
      в конфиг/дискавери-слой (см. 3.1).
      **ЗАКРЫТО 2026-10-02:** перенос выполнен — функция живёт в
      `mcp-platform/libs/vectors/runtime.py:28` и по контракту **не**
      открывает файл снимка сама (см. docstring там же).
- [x] 3.3 `cli.py:245` `if hasattr(provider, "open_cache")` — убрать `hasattr`:
      duck-typing по методу, которого нет в интерфейсе, и есть признак
      протечки. Ошибка MUST NOT конструироваться вручную из булева
      результата open, verify тест: сбой открытия не маскируется под
      «файл не найден»
- [x] 3.4 `cli.py:242` docstring «Открыть DuckDB-кэш через CacheProvider» →
      формулировка от реализации («DuckDB-кэш») к интерфейсу
- [x] 3.5 `skill_config.py:68-76` `get_in_memory_cache_path()` — docstring
      «Путь к DuckDB-кешу skill'а» убрать; функция используется для
      сообщения об ошибке, сам путь в слое skill'а не нужен, verify
      оставшийся вызов работает и не раскрывает путь хранения

## 4. Мёртвый код и ложные docstring'и, порождённые старой моделью

- [x] 4.1 `duckdb_cache_store.py:381-391` instance-метод `open(self)`
      **перекрыт** `@classmethod open(cls, path, mode)` (`:432-459`) в теле
      того же класса и потому недостижим; удалить вместе с ложным docstring,
      называющим callers `gateway.py` и `benchmarks/runner.py` (оба зовут
      `connect()`). verify `grep -n "def open" lib/services/duckdb_cache_store.py`
      даёт ровно одно определение
- [x] 4.2 `duckdb_cache_store.py:925-928` «Gateway НЕ держит его открытым» —
      ложь, противоречит `:1556-1559` и собственному `:1079`. Правка — в
      `cache-architecture-alignment` §5.8 вместе с удалением
      `publish()`-как-self-replace; здесь — зафиксировать в guard'е, что
      docstring не должен утверждать модель владения файлом
- [x] 4.3 `preload_service.py:18-21` «in-memory mirror → snapshot file» и
      «CLI-агент остаётся чистым читателем» — обе части неверны
      (`_cache_path == _publish_path`); docstring приводится в соответствие
      после §5.8

## 5. Тесты

- [x] 5.1 `tests/test_skill_tool_independence.py` (существующий) остаётся
      зелёным; verify
- [x] 5.2 Новый guard `tests/test_single_cache_interface.py`: **ровно одна**
      реализация `CacheProvider` в репозитории (никаких вторых подклассов);
      ни один модуль под `workspace/skills/` не содержит `cache_provider_impl`,
      `duckdb.connect`, `DuckDbCacheStore(`, `PostgresDuckDbProvider(`,
      `open_cache`, прямого чтения `gateway.cache.local_path`; verify guard
      проходит и падает на заведомо добавленном нарушении
- [x] 5.3 Регресс на расхождение путей: `gateway.cache.local_path` как
      директория разрешается одинаково в `resolve_publish_path` и в потребителях
- [x] 5.4 Регресс на относительный `local_path` (бывший silent `[]`)
- [x] 5.5 Регресс: недоступный файл отличается от пустого результата
- [x] 5.6 Полный прогон без новых падений: baseline на чистом `0ad31d6` и на текущем
      дереве дают **идентичный** набор из 8 падений (3 × `flush_interval`,
      `hook loader syspath`, `acceptance_matrix`, `information_preservation` ×2,
      `resume_scenarios`) — все воспроизводятся без этого change.
      Замер сделан интерпретатором из `requirements.txt` (nanobot 0.3.5):
      3848 passed → 4052 passed. ВАЖНО: репозиторный `.venv` содержит
      nanobot **0.3.0** и даёт ложные падения (`nanobot.agent.tools`,
      `tool_registry`) плюс 8 «TestCreate» — это артефакт окружения,
      а не регрессия.

## 6. Документация и проверки

- [x] 6.1 `docs/architecture/decisions/audit-analyzer-runtime-boundary.md` —
      сверить с фактическим состоянием после выполнения.
      **ВЫПОЛНЕНО 2026-10-02:** ADR получил приписку о снятых символах
      (таблица «что с ним стало»), таблица «Проверка соответствия» приведена
      к факту, а несуществующие сторожа помечены снятыми. Исторические
      разделы («Контекст», «Baseline», «Хронология») сохранены как запись о
      том, какой была подсистема и почему её снесли — ADR переписывать нельзя.
- [x] 6.2 `workspace/skills/audit_analyzer/SKILL.md` § «Runtime boundary» —
      убрать формулировки, привязывающие skill к DuckDB.
      **ВЫПОЛНЕНО 2026-10-02:** grep по SKILL.md на `DuckDB`/`duckdb` даёт
      0 hit. Раздел «Граница навыка» явно перечисляет, чем навык **не**
      владеет и что всё это — платформа. Сделано фазой 9
      `enterprise-mcp-platform`; страж
      `tests/test_audit_analyzer_skill_doc.py` проверяет это на каждом
      прогоне (211 тестов в связке с `test_skill_tool_independence.py`).
- [x] 6.3 `docs/skill-tool-architecture.md` §2 — пример импорта
      `from lib.services.cache_provider_impl import build_cache_provider`
      заменить на интерфейсный вызов.
      **ВЫПОЛНЕНО 2026-10-02:** пример переписан на клиент платформы; рядом
      оставлена заметка о прежней форме и о том, куда уехали оба модуля.
      Попутно сняты ещё две ссылки того же рода: `CacheProvider.query_sql`
      в описании шага конвейера и перечисление снесённых модулей в правиле
      про `label`.
- [x] 6.4 `python tools/architecture_guard.py` — без новых нарушений (exit 0)
- [x] 6.5 `python tools/diagnose_startup.py` — OK
      **ВЫПОЛНЕНО 2026-10-02 по логу реального старта** (`gateway.py
      --profile=test`, реальный PostgreSQL, реальный `enterprise-mcp`).
      Первый прогон дал **exit 1 и девять ложных CRITICAL/DRIFT** на
      полностью здоровом старте. Три причины найдены и устранены:

      1. **Баннер патчей не доходил до лога вовсе.**
         `application_context.py` печатал его через stdlib `logging` на
         уровне INFO, а эффективный уровень логгера
         `lib.core.application_context` — WARNING. Сообщение отбрасывалось
         молча, и инструмент читал старт как «патчи не применялись»
         (`CRITICAL MISSING REQUIRED: ['context_governor', 'assemble_outbound',
         'subagent_logging']`). Баннер выведен в stdout тем же путём, что и
         «Hooks connected».
      2. **Список хуков переносился по ширине консоли.** `rich` печатал
         семь хуков в трёх физических строках, а парсер — построчно —
         видел два и рапортовал `MISSING REQUIRED` по трём хукам и по
         hook factory. Включён `soft_wrap=True`: диагностический лог обязан
         оставаться машинно-читаемым.
      3. **Блок патчей не имел терминатора** и ждал пустой строки, которой в
         реальном выводе нет. Статусные строки старта (`✓ DB pool`,
         `✓ PostgreSQL channel enabled`, `✓ Channels enabled`,
         `✓ enterprise-mcp:`) засчитывались как патчи — ложный
         `DRIFT UNEXPECTED APPLIED`. Парсер теперь закрывает блок по первой
         непохожей строке, а эмиттер печатает пустую строку-разделитель.

      Итоговый прогон: **OK** по всем четырём разделам (хуки, built-in
      tools, project tools, runtime patches), **exit 0**. Регрессионные
      тесты на фикстуре реальной формы лога добавлены в
      `tests/test_diagnose_startup.py` (три новых кейса).
- [x] 6.6 Запись в `CHANGELOG.md` `[Unreleased]`.
      **ВЫПОЛНЕНО 2026-10-02:** две записи — про ложные CRITICAL
      `diagnose_startup.py` (с тремя причинами) и про правку документации,
      описывавшей снесённую подсистему.

## Вне scope этого change

- **Сам механизм process-exclusivity** (кэш как неделимая ресурс-запись,
  фатальность конфликта на старте, запрет отдельного pre-check занятости) —
  `cache-architecture-alignment` §5.12-5.16. Здесь снимается только следствие
  для skill'а: ручное конструирование «cache not found» вместо разбора
  реальной причины не удаётся.
- Ядро интерфейса (наследование `DuckDbCacheStore`, судьба
  `refresh`/`check_stale`, удаление `PostgresDuckDbProvider` и
  `publish()`-как-self-replace, типизация `ctx.cache_provider`) —
  `cache-architecture-alignment` §5.
- ownership, heartbeat, fencing, producer lifecycle, типизированные
  storage-ошибки, удаление alias `cache_store` — `cache-architecture-alignment`.
  Заметка: `CacheOwnershipCoordinator` **сохраняется** — за координацию
  записи/синхронизации, а не за передачу открытого файла между процессами.
- Создание `workspace/tools/audit_analyzer_query.py` и удаление
  `scripts/cli.py` — **отменено**: противоречит принятому эталону
  (эпоха A/G, CLI-слой) и `docs/skill-tool-architecture.md` §8, а также
  завело бы второй путь доступа к файлу вместо интерфейса.
