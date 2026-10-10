# Tasks: дыра канона `data/vector-indexes`

## Легенда

- **[O]** — владелец канона / решение по спецификациям.
- **[A]** — агент (`C:\Users\Алексей\.nanobot`, дерево `lib/`, `config.json`).
- **[P]** — платформа (`mcp-platform/`, capability `vectors`).
- **НЕ ПРОВЕРЕНО** — утверждение получено чтением кода (grep / чтение файлов), а не
  запуском. Все ссылки на строки получены поиском по дереву.

## 1. Спецификация (этот change)

- [x] 1.1 **[O]** Разобрать `openspec/specs/data/vector-indexes/spec.md` по
  требованиям. Их 8; все 8 опираются на код, которого нет в дереве.
- [x] 1.2 **[O]** Сверить каждое требование с фактическим владением: 8 требований,
  6 файлов реализации, отсутствуют все (`vector_index_service.py`,
  `cache_provider.py`, `cache_provider_impl.py`, `preload_service.py`,
  `infra_registration.py`, `build_vectors.py` — 6 из 6).
- [x] 1.3 **[O]** Классифицировать: 4 `REMOVED` (прошлый мир), 3 `MODIFIED`
  (намерение остаётся, владелец изменился), 1 `ADDED` (молчание о настоящем).
  Первоначально было 5 `REMOVED`: требование «Прогрев индексов при старте» выведено
  из снятых — обоснование держалось на ложных докстрингах `owner.py:8-11,305-311`,
  исправленных коммитом `e81228e`; тело переписывает
  `../2026-10-05-vector-preload-canon/`.
- [x] 1.4 **[O]** Проверить, что дыру не закрыл никто: у
  `2026-10-04-close-cache-provider-canon-gap` есть только
  `specs/data/cache-provider/`, `vector-indexes` — нет. Проверено обходом дерева.
- [x] 1.5 **[O]** Сверить на пересечение: соседняя дельта про `cache-provider`
  содержит требование «владелец векторных индексов отделён от владельца снимка»,
  но НЕ объявляет `platform.json → vectors.indexes` и НЕ упоминает
  `gateway.vector`. Дублирования нет; оттуда взят только факт ленивой постройки.
- [x] 1.6 **[O]** Написать дельту: 1 `ADDED`, 3 `MODIFIED`, 4 `REMOVED`.

## 2. Открытое решение — проза канона вне `## Requirements`

- [x] 2.1 **[O]** `data/vector-indexes` содержит **15** прозуических разделов вне
  `## Requirements` (H2 в `openspec/specs/data/vector-indexes/spec.md`: `Purpose`:3,
  `Scope`:7, `Responsibility`:12, `Boundary`:20, `Public Contract`:43,
  `Forbidden Behavior`:184, `Dependencies`:194, `Configuration`:202, `Lifecycle`:227,
  `State`:235, `Invariants`:242, `Error Behavior`:249, `Consumers`:255,
  `Implementation`:261, `Verification`:271), и все они называют несуществующие
  файлы. **Список исчерпывающий** — раньше здесь были перечислены только разделы
  *после* `## Requirements`, из-за чего пять молча выпали:

  **Предшествуют `## Requirements` (`:50`) — читаются как актуальный контракт, и
  два из них прямо противоречат нормативному тексту дельты:**

  * `## Purpose`:3 — «предоставляются через `CacheProvider.search_vector`»
    (сервиса нет);
  * `## Scope`:7 — единственное место, где переезд уже описан правильно; конфликта
    с дельтой нет, но и **правки не требует** — фиксируем как есть;
  * `## Responsibility`:12 — «через CacheProvider», «FAISS-backed хранение»;
  * `## Boundary → Owns`:23 — «конфигурацией векторных индексов в config.json».
    **Противоречит дельте:** владение конфигурацией ушло в
    `platform.json → vectors.indexes`;
  * `## Public Contract`:45-48 — «VectorIndexService предоставляет … загрузку
    конфигурации индексов из config.json … сборку FAISS индексов через
    build_vectors.py … поиск … через CacheProvider.search_vector». **Противоречит
    дельте по всем трём пунктам** (сервиса, сборщика и хранилища поиска нет).

  **Следуют за `## Requirements` — описывают прошлый мир, дельтой не закрываются:**

  * `## Forbidden Behavior`:184 (`CacheProvider.search_vector`, legacy-таблица
    `public.agent_vector_index_config`), `## Dependencies`:194,
    `## Configuration`:202 (пример JSON с `gateway.vector.index.indexes` — см. 2.2),
    `## Lifecycle`:227 (шаг «build_vectors.py строит FAISS индексы»), `## State`:235
    (`VectorIndexService` хранит пути к FAISS-файлам), `## Invariants`:242
    («Конфигурация читается только из config.json»), `## Error Behavior`:249,
    `## Consumers`:255 (AuditAnalyzer, LegalSummarizer), `## Implementation`:261,
    `## Verification`:271.

  **Нужно решение, а не молчание:** расширять формат дельт до разделов либо
  править прозу канона напрямую. Заложено: **правка прозы вручную тем же
  change'ом**, что и дельта, чтобы канон не остался в промежуточном состоянии.
  `## Public Contract` и `## Boundary → Owns` — приоритетные: они стоят **до**
  `## Requirements`, поэтому после архивации читаются как действующий контракт и
  прямо зовут вернуть то, что дельта удаляет. `## Scope` при этом не трогаем:
  он единственный уже верный. Тот же вопрос стоит перед
  `2026-10-04-close-cache-provider-canon-gap` (задача 2.2 в его `tasks.md`).
- [x] 2.2 **[O]** `## Configuration` в каноне — самый опасный раздел: он
  показывает валидный на вид JSON с `gateway.vector.index.indexes.<name>.dimension`.
  Такого ключа в `config.json` нет (0 совпадений), и `signature_table`, на который
  ссылаются три требования, тоже отсутствует (0 совпадений в `config.json` и в
  `lib/`). Пример неверен дважды — по владельцу и по схеме. Реальное объявление:
  `table`, `pk`, `source_table`, `content_columns`, `embedding_columns`,
  `track_column`, `chunk_size`, `chunk_overlap`, `metric`, `enabled`. После удаления
  секции этот пример станет инструкцией «так объявляйте», которой нельзя
  следовать. Правка обязательна, а не косметическая.

## 3. Удаление мёртвого дубля конфигурации

- [x] 3.1 **[A]** Удалить `gateway.vector.index` из `config.json` (целиком, включая
  `indexes`, `storage_table`, `default_root`, `backend`, `enable`).
- [x] 3.2 **[A]** Удалить `skills.audit_analyzer.tables` и
  `skills.audit_analyzer.vector_indexes` из `config.json` — обе секции дублируют
  `platform.json → audit.tables` и `platform.json → vectors.indexes`, потребителей
  в `lib/` нет.
- [x] 3.3 **[A]** Удалить из `lib/core/project_settings.py`: `VectorIndexConfig`
  (`:481`), `VectorInfrastructureSettings` (`:186`), `VectorIndexSettings`
  (`:141`), `TableEntry` (`:418`), `VectorIndexEntry` (`:449`), поля
  `SkillSettings.tables` (`:666`) и `SkillSettings.vector_indexes` (`:667`) и
  поле `vector` из контейнера (`:246`).
  `VectorIndexSettings` в перечне раньше не было, хотя именно она держит
  `storage_table` (`:182`), `indexes: dict[str, VectorIndexConfig]` (`:183`) и
  docstring со ссылками на `gateway.vector.index.*` (`:151,158`; рядом `:152` —
  legacy-путь `gateway.vector_index.*` **без точки**, а `:162` — строка вообще
  без ссылки на путь, только «См. `docs/MIGRATION.md` и
  `docs/VECTOR_INDEXES.md`», поэтому в перечень ссылок на
  `gateway.vector.index.*` они не входят); вне своего docstring'а и объявления
  она не используется, так что удаляется целиком.
  Два поля `SkillSettings` — **единственные** использования `TableEntry` /
  `VectorIndexEntry` вне их объявлений, поэтому без их удаления удаление самих
  классов невозможно. Побочный эффект, важный для дельты: пока поля объявлены,
  `extra="forbid"` модели `SkillSettings` (`:663`) их **типизирует, а не
  отвергает** — секция `skills.*` валидируется (`config.json:817,835`), и
  отвержение появляется ровно этим удалением.
- [x] 3.4 **[A]** Проверить **следствие** 3.3, а не заданное условие: после
  удаления полей `SkillSettings.tables` и `SkillSettings.vector_indexes`
  (сделано в 3.3) секция навыка обязана остаться валидируемой
  (`enabled`, `cli`, `llm`), иначе `config.json` перестанет проходить разбор.
- [x] 3.5 **[A]** Обновить `tests/test_config_keys.py`: вместо ассертов со
  значениями (`:90-101, :146-159`) утверждать **отсутствие** секций. Диапазоны
  взяты по **фактическим блокам**, а не по началу: `:88-97` обрывается **внутри**
  `vector_indexes` и не покрывает `:98-101` (три имени индекса), а `:146-150`
  покрывает 5 ассертов из 14 (`:146-149` — `enable`, `default_root`, `backend`,
  `storage_table`; далее по 4/3/3 на `audits_index`, `violations_index`,
  `audit_reports_index`). Исполнитель, взявший диапазон буквально, оставит 9 живых
  ассертов на удаляемые ключи и задачу по существу не выполнит; спрятать это
  негде — страж 4.6 `tests/` намеренно не обходит, то есть недобранный хвост не
  поймает никто, а оставшиеся ассерты печатают значения удаляемых секций как
  ожидаемые — ровно то, что `proposal.md` называет корневой проблемой change'а.
  * Отдельно — комментарий `:114-118`: 15-е совпадение `gateway.vector.index` в
    этом файле и **вне** обоих диапазонов. Это не ассерт, но **живое утверждение**
    — «Индексы декларируются в `gateway.vector.index.indexes`»; после удаления
    секции оно станет ложным, то есть это тот же класс дефекта, который change
    лечит, только внутри задачи, которая его удаляет. Переписать формулировку на
    «индексы объявляет платформа, `platform.json → vectors.indexes`»
    (`mcp-platform/platform.json`); без отдельного пункта комментарий переживёт
    удаление и будет уводить читателя искать несуществующую секцию.
- [x] 3.6 **[A]** Обновить `tests/test_project_settings.py`: снять модульный
  импорт `TableEntry` (`:9-13`) и удалить четыре класса — `TestTableEntry` (`:121`),
  `TestVectorIndexEntryNoSource` (`:389`, локальные импорты `VectorIndexEntry` на
  `:401,412,420`), `TestGatewayVectorIndexConfig` (`:446`, валидирует
  `gateway.vector.index.indexes` и читает
  `result.gateway.vector.index.indexes["audits_index"]` на `:479`),
  `TestTableEntryTypeLiteral` (`:597`); поправить упоминание
  `VectorInfrastructureSettings` на `:533`. Проверять надо именно этот файл:
  импорт на уровне модуля означает `ImportError` при collection, то есть падает
  **весь** файл, а не один тест. Побочный эффект, который стоит закрыть здесь же:
  `TEST_VECTOR_TABLE` в `tests/conftest.py` после удаления последнего
  пользователя станет неиспользуемым — либо удалить, либо оставить с
  пояснением.
- [x] 3.7 **[A]** Вычистить упоминания `gateway.vector.index`, которые **не**
  уходят вместе с моделями. Сейчас в `lib/` 7 совпадений: `:151,158` (docstring
  `VectorIndexSettings`), `:456,470,482` (уходят с моделями), **`:651`**
  (docstring `SkillSettings`, **не** `SkillsSettings` — тот объявлен на `:675`) и
  **`:784`** (текст в `_LEGACY_GATEWAY_KEYS`, `:782-784`) — эти два остаются.
  В `tools/` — 2 совпадения, и **оба остаются**:
  `tools/legacy_audit.py:35` (docstring) и `:377` (отчёт по legacy-ключам; сам
  список `_LEGACY_CONFIG_KEYS` на `:120-121` трогать нельзя — это fail-fast для
  legacy-пути **без точки**, см. 5.3). Без отдельной задачи они переживут удаление
  и будут уводить читателя искать несуществующую секцию.
  Сюда же — `register_vector_storage`: единственное оставшееся совпадение в `lib/`
  — docstring `_reject_legacy_renamed_sections` на `:263` (упоминает несуществующий
  уже consumer).

  **Отдельно — `config.py` в корне репозитория.** Это исполняемый код агента
  (именно он читает `config.json`), но ни одна задача этого change его не трогает,
  а страж 4.6 без него зелёный. Поэтому живое утверждение «
  `gateway.vector.index.*` — единственный канонический путь» (`:187`) переживёт
  удаление и будет прямо спорить с требованием «Агент ДОЛЖЕН NOT держать собственный
  список индексов… ни в `gateway.*`, ни в `skills.*`». Вычистить:
  * `:87,118,120,187` — ссылки на путь как на канонический (`:187` — «единственный
    канонический путь»);
  * `:90` — ссылка на `project_settings.py::TableEntry`, класс удаляется в 3.3;
  * `:185` — ссылка на `VectorIndexSettings` / `VectorIndexConfig`, обе модели
    удаляются в 3.3;
  * `:80-120` — документация живой схемы секции: `tables` / `vector_indexes` поданы
    как действующие OPTIONAL-секции, структура `tables` расписана как актуальная.
  По обязательным нулём токенам в `config.py` одно совпадение — `TableEntry` на
  `:90`; `build_vectors`, `register_vector_storage`, `gateway_vector_index_config`
  и `CacheProvider.search_vector` — 0 совпадений.
- [x] 3.8 **[A]** Обновить `AGENTS.md` и `mcp-platform/platform.json`
  (`_about`-пояснение про дубликат для `build_vectors.py`): файл сборки в дереве
  отсутствует, оправдание дубля больше не действует.
- [ ] 3.9 **[A]** **Решение, а не правка по умолчанию:** определить, чем именно
  отвергается `gateway.vector.index` после удаления поля. Механизма в ветке
  `gateway.*` нет — `_StrictOptional` = `extra="allow"`
  (`lib/core/project_settings.py:55` — это `model_config = ConfigDict(extra="allow")`
  в базовом `_StrictOptional`), `GatewaySettings` (`:176`) и
  `ProjectSettings` (`:593`) своего `model_config` не имеют. Готовый образец в
  дереве есть — валидатор `_reject_legacy_renamed_sections` (`:193`) поверх
  `_LEGACY_GATEWAY_KEYS` (`:619`), но он знает только `gateway.vector_index`
  **без точки**, то есть legacy-путь, а не удаляемую секцию. **НЕ ПРОВЕРЕНО:**
  выбор между (а) внести текущий путь в legacy-guard как fail-fast и (б) иным
  способом запретить секцию — не принимался; без решения требование дельты
  «секция отвергается» невыполнимо, и `config.json` примет ключ молча.

- [x] 3.10 **[A]** **Состав индексов не тиражируется в навык — его спрашивают у
  платформы.** Пока `list_indexes` не был объявлен модели, имена индексов жили
  таблицей в `workspace/skills/audit_analyzer/SKILL.md`. Это копия объявления
  `platform.json → vectors.indexes`, и дельта 3.1-3.3 удалила вторую копию из
  `config.json`, оставив навыковую на её месте: то есть после удаления мёртвого
  дубля тиражирование продолжилось, просто в другом файле. Симметрично с 3.1
  объявлена модельной операция: `list_indexes` добавлена в
  `config.json → tools.mcpServers.enterprise.enabled_tools`, а навык переписан —
  таблица имён заменена вызовом, имена берутся из ответа.
  Три вещи обязаны были уехать вместе, иначе одна из них смысла не имеет:
  * `tests/test_mcp_platform_declaration.py::EXPECTED_TOOLS` — 7 → 8, вместе с
    объяснением, почему именно эта операция (восьмая не «ещё одна диагностика»);
  * `tests/test_audit_analyzer_skill_doc.py` — правило каталога индексов
    **инвертировано**: было «каждый объявленный индекс обязан быть описан в
    навыке», стало «навык не перечисляет имён индексов». Прежнее правило не
    отменяло копирование, а заставляло повторять его — стражи ловили расхождение
    уже после того, как навык вручную привели в согласие с платформой. Рядом
    добавлена вторая половина: `list_indexes` обязана остаться в
    `enabled_tools`, иначе первая стража зелёная, а у модели нет ни одного
    способа узнать имя индекса;
  * `ROUTED_OPERATIONS` навыка и `test_no_unrouted_operation_is_promised`:
    `list_indexes` переходит из «обещать нельзя» в «объяснять надо», запрет
    остаётся на `index_stats` — состояние индекса модели и так доступно.
  `index_stats` намеренно **не** выдана: каталог отдаёт `list_indexes`, а
  состояние конкретного поиска — `index_state` в ответе `vector_search`, то есть
  третий способ узнать то же самое модели не нужен.

  **Число «7» осталось в трёх местах — и это не пропуски, а чужие записи.**
  Обойдено `openspec/`, `docs/`, `mcp-platform/` и `AGENTS.md`; правлены
  `AGENTS.md:28`, `docs/ARCHITECTURE.md:19`,
  `mcp-platform/docs/MCP-CONTRACTS.md:1522`, `docs/skill-tool-inventory.md:28`,
  `docs/SKILL_AUTHORING.md:565` — это проза о **настоящем**, и после 3.10 она
  ложна. Не тронуты: `2026-10-05-wire-mcp-provider/tasks.md:44` («в реестр
  пришло 7 операций») и `2026-10-03-mcp-native-tools/tasks.md:40` — это
  записи о прогонах и приёмке, то есть факты о конкретном моменте; править их
  значило бы переписать чужое наблюдение. `2026-10-05-call-boundary-invariants/
  proposal.md:160` — проза соседнего **незакрытого** change'а о текущей границе;
  он не архивирован, и решение по нему принадлежит его владельцу, а не этому
  change'у.

## 4. Проверка

- [x] 4.1 **[A]** `tests/test_config_keys.py` — отдельно
- [x] 4.2 **[A]** `tests/test_enterprise_mcp_config.py` — отдельно. Замечание:
  формулировка «схема `ProjectSettings`, `extra="forbid"`» была неверной —
  `ProjectSettings` объявлен `extra="allow"`
  (`lib/core/project_settings.py:596`); файл проверяет разбор конфигурации, а не
  отвержение неизвестных ключей на корне.
- [x] 4.3 **[O]** `python tools/validate_component_specs.py --strict` — baseline
  = **ровно 3 нарушения, все три в
  `openspec/specs/runtime/entrypoints/spec.md`** (требования без сценариев); в
  `data/cache-provider` и `data/vector-indexes` — 0. Число не должно вырасти.
  **Оговорка:** валидатор обходит только `openspec/specs/`
  (`tools/validate_component_specs.py:51,439`) — дельты в `openspec/changes/` он
  **не проверяет**, поэтому зелёный прогон не означает, что дельта этого change
  структурно корректна.
- [x] 4.4 **[A]** `py_compile` по `lib/core/project_settings.py`
- [x] 4.5 **[A]** `tests/test_project_settings.py` — отдельно (см. 3.6: файл падает
  целиком на модульном импорте `TableEntry`, а не на одном тесте)
- [x] 4.6 **[A]** Grep-страж **по исполняемому коду**, а не «0 совпадений вообще».
  Требование «0 совпадений в `lib/`, `tools/`, `openspec/specs/`» недостижимо и
  потому неисполнимо: легальные упоминания переживут удаление по построению.

  **Область обхода — агентская часть, список путей явный:** `lib/`, `tools/`,
  `gateway.py`, `cli_agent.py`, `config.py`, `workspace/`. `config.py` назван
  рядом с `gateway.py` и `cli_agent.py` не по каталогу, а по существу: это те же
  корневые модули агента, и именно `config.py` читает `config.json`, то есть
  файл, который меняет 3.1. Без него заголовок «по исполняемому коду» неполон, а
  3.7 перечислял только `lib/` (7 совпадений) и `tools/` (2) — страж оставался
  зелёным при живом утверждении «`gateway.vector.index.*` — единственный
  канонический путь» (`config.py:187`). Без области фраза «весь исполняемый
  код» неисполнима: `build_vectors` легально живёт в коде платформы, и grep по
  всему репозиторию даёт в исполняемом коде 5 совпадений, которых нет ни в одном
  исключении — `mcp-platform/libs/vectors/builder.py` (2),
  `mcp-platform/libs/enterprise_common/settings.py` (1),
  `mcp-platform/servers/enterprise/build_index.py` (1),
  `mcp-platform/tests/test_vector_builder.py` (1); вне области остаются также
  `mcp-platform/docs/TARGET-ARCHITECTURE.md` (4) и
  `mcp-platform/platform.json:125` (1).

  **0 совпадений обязательно:** `gateway_vector_index_config`,
  `build_vectors`, `register_vector_storage` (в `lib/` остаётся ровно одно — в
  docstring'е `_reject_legacy_renamed_sections`,
  `lib/core/project_settings.py:201`; вычистить, см. 3.7), `CacheProvider.search_vector`.

  **Допустимые исключения, перечисленные явно. Список шире области обхода выше:**
  часть пунктов лежит внутри области, часть — вне неё; ниже они разделены, чтобы
  не выдавать внешние за внутренние. Исключение — надмножество: лишний пункт
  безопасен, пропущенное совпадение — нет.

  *Внутри области обхода:*
  * docstring'и, описывающие legacy-путь: `project_settings.py:151,158,456,470,482`,
    `:651`, `:784` (последние две — обязательная правка по 3.7), `tools/legacy_audit.py:35`;
  * сам legacy-guard `_reject_legacy_renamed_sections` / `_LEGACY_GATEWAY_KEYS`
    (`:193`, `:619`) — он существует именно чтобы называть старый путь;
  * `tools/legacy_audit.py:377` — отчёт по legacy-ключам, перечисление путей
    назначения (решение «оставить или удалить» — 5.3);
  * `workspace/memory/history.jsonl:50` — 1 совпадение, `workspace/` в область
    входит: это append-only журнал памяти агента, запись не переписывается и
    совпадение означает найденный текст, а не живое утверждение о пути.

  *Вне области обхода, но известны и учтены — `tests/`, `openspec/specs/` и
  `openspec/changes/` в список путей области не входят:*
  * `tests/test_docs_consistency.py:299` — путь `tools/build_vectors.py` в списке
    проверяемых документов; файл удалён, проверка обязана это отражать;
  * `gateway.vector.index` в `openspec/changes/` этого change — описание
    снимаемого поведения, а не рабочий код;
  * исторический `openspec/specs/OWNERSHIP.md` и явно помеченный архив — в
    список путей области (`:200-201`) файл не входит, поэтому остаётся как есть;
    иначе он был бы записан внутри области и классификация стала бы неверной
    буквально.

  Проверка: прогон перечисленных строк и объяснение каждого совпадения, а не
  молчание.

## 5. Открытые решения — НЕ в этом change

- [x] 5.1 **[P]** Сводка здоровья индексов посчитана, но не публикуется нигде
  (`mcp-platform/libs/vectors/preload.py:30,65`, экспорт — `libs/vectors/__init__.py:50`,
  в `__all__` `:69` / `:71`; производственных вызовов нет).
  **Решение владельца: снять требование.** Требование «Preload health-summary виден
  оператору и логируется» снято дельтой этого change'а (`## REMOVED Requirements`).
  Публикация не возвращается без нового change, у которого есть адресат (оператор
  или capability `data`) и порог. Расчёт НЕ удалён вместе с требованием.
- [x] 5.1a **[O]** Второй канон, требовавший то же событие, **больше его не
  требует** — открытого вопроса тут не осталось. Живой
  `openspec/specs/observability/logging-db/spec.md` не содержит ни строки
  `vector_index_preload_health`, ни сценария «preload health-summary через
  DbLoggingService»: требование «Sync-события через DbLoggingService» снято архивным
  change `2026-10-05-logging-db-dead-producers` (`## REMOVED Requirements`). В
  `data/vector-indexes` то же требование снимает дельта этого change'а. Провисшего
  требования не осталось: публиковать было некому в обоих канонах, и не требуется
  ни в одном.
- [x] 5.2 **[O]** `COMPONENTS.md` перечисляет `CacheProvider` и
  `VectorIndexService` как компоненты агента и указывает файлы, которых нет.
  **Расхождения в дереве не оказалось — пункт закрыт проверкой, а не правкой.**
  Поиск `VectorIndexService` по `openspec/specs/COMPONENTS.md` даёт ноль
  совпадений, а строка `CacheProvider` указывает на живой файл платформы
  `mcp-platform/libs/enterprise_data/snapshot/contracts.py:CacheProvider`.
  Сплошная проверка всех строк реестра (59 ссылок) нашла единственное реальное
  расхождение, и оно было другим: строка `Profiles` объявляла источник
  `config.json::profiles`, тогда как профили объявляет
  `mcp-platform/platform.json::profiles`. Исправлено коммитом `3bf1a8c`.
  Вывод «`COMPONENTS.md` перечисляет несуществующие файлы агента» был верен
  для состояния до 2026-10-01 и устарел.
- [ ] 5.3 **[A]** `tools/legacy_audit.py:121` перечисляет `gateway.vector_index`
  в списке legacy-ключей. После удаления секции запись в списке либо остаётся
  осмысленной (проверка регрессов), либо становится мёртвой — решение при правке.
- [x] 5.4 **[P]** `mcp-platform/tests/test_vectors_index_health.py` проверяет
  расчёт, у которого нет производственного вызова. Прежний текст ждал, что после
  5.1 вызов появится; **ожидание отменено** — решение владельца публикацию не
  возвращает, поэтому вызова не будет и дальше. Объявление «оставлено осознанно,
  удалять нельзя» добавлено в докстринг самого теста
  (`mcp-platform/tests/test_vectors_index_health.py:1-21`): названа причина, назван
  change, снявший требование, и сказано прямо, что сносит страж как «мёртвый»
  охрана инвариантов stale / orphan / missing. Смысл 5.4 был в риске снести страж
  как мёртвый — риск снят объявлением в коде, а не текстом задачи.
