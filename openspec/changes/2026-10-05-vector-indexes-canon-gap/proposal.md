# Канон `data/vector-indexes` обещает агенту чтение из config.json, которого не существует

## Why

Спека `openspec/specs/data/vector-indexes/spec.md` устарела в корне: её
`## Scope` описывает переезд в capability `vectors` (фраза «агент индексы не
строит и не хранит» есть дословно), а `## Requirement` и разделы ниже требуют
обратного — «Система ДОЛЖНА читать конфигурацию векторного индекса только из
`gateway.vector.index.indexes.*` в `config.json`».

Внутри одной спеки утверждение и его отрицание стоят рядом. Спека при этом
перечисляет файлы реализации, которых в дереве агента нет:

| Файл из спеки | На диске |
|---|---|
| `lib/services/vector_index_service.py` | нет |
| `lib/services/cache_provider.py` | нет |
| `lib/services/cache_provider_impl.py` | нет |
| `lib/services/preload_service.py` | нет |
| `lib/core/infra_registration.py` | нет |
| `tools/build_vectors.py` | нет (есть только в `.worktrees/fork-…`) |

Все шесть удалены вместе с кэш-кластером (`drop-local-cache-read-from-pg`,
`AGENTS.md`). `OWNERSHIP.md:28` уже зафиксировал переезд и назвал преемника —
`mcp-platform/libs/vectors/`, объявления в `platform.json → vectors.indexes` — но
тело спеки за переездом не пошло, потому что переезд объявляли в другом документе.

Три требования спеки при этом не выполнены нигде, и это не «документ отстал»
(про прогрев — см. оговорку ниже: он как раз выполнен):

* **Требует отдельной команды `build_vectors`** для сбора индекса. Сборка в
  платформе идёт сама, на старте сервера, до event loop
  (`servers/enterprise/server.py::_prepare_capabilities` → `ensure_index`).
* **Требует прогрева до приёма запросов, запрет холодного поиска и публикацию
  сводки в `stderr` и журнал `agent_gateway_logs`.** Первая половина **выполнена**:
  `_prepare_capabilities` собирает все объявленные и включённые индексы до старта
  event loop, холодного поиска нет. Вторая — нет: сводка посчитана
  (`mcp-platform/libs/vectors/preload.py:22,57`) и экспортирована
  (`libs/vectors/__init__.py:47`; в `__all__` — `compute_index_health` на `:66`
  и `format_index_health_lines` на `:68`), но **производственного вызова нет ни
  одного** — только `mcp-platform/tests/test_vectors_index_health.py`. Порога
  устаревания в требовании **нет**: в `openspec/specs/data/vector-indexes/spec.md`
  на доли устаревших 0 совпадений, в `mcp-platform/libs/vectors/` — 0 совпадений по
  `0.3` / `stale_ratio`; единственный близкий пункт канона — «оператор SHALL увидеть
  `level=WARN` (вместо `INFO`)» при неполном прогреве (`:172`), без числа. Числовой
  порог, если он нужен, — отдельное требование с владельцем, а не свойство снятой
  спеки.
* **Требует `default_root` и `storage_table` как владельца FAISS-хранилища** и
  связывает их с блоком агента. Снимком владеет capability `data`, а состав
  индексов объявляет capability `vectors` (`platform.json → vectors.indexes`).

### Мёртвый дубль, который канон закрепляет

`config.json` всё ещё содержит `gateway.vector.index.*` и
`skills.audit_analyzer.vector_indexes` — то же самое, что в
`platform.json → vectors.indexes`, плюс `skills.audit_analyzer.tables`, совпадающее
с `platform.json → audit.tables`. Потребителей в дереве агента нет:

* `VectorInfrastructureSettings` объявлена в `lib/core/project_settings.py:186`, но
  в `lib/` её никто не читает — grep даёт только объявления и docstring-комментарии;
* `VectorIndexSettings` (`:141`) — модель, которая реально держит `storage_table`
  (`:182`) и `indexes: dict[str, VectorIndexConfig]` (`:183`) и docstring со
  ссылками на `gateway.vector.index.*` (`:151,158`; `:152` — это legacy-путь
  `gateway.vector_index.*` **без точки**, а `:162` — строка вовсе без ссылки на
  путь, поэтому в перечень ссылок на `gateway.vector.index.*` они не входят);
  вне этого своего docstring'а и объявления она тоже не используется, поэтому
  удаляется целиком вместе с остальными моделями;
* `skills.audit_analyzer.*` читает только `tests/test_config_keys.py:90-101`;
* `VectorIndexConfig` при этом называет свою секцию «единственным источником деталей
  построения индекса» (`project_settings.py:484`).

То есть мёртвая конфигурация не просто лежит — она **описана в коде как
единственный источник истины** и **держится в живых тестах ассертами, печатающими
её значения как ожидаемые**. Через месяц никто не будет знать, какой из двух
списков индексов настоящий. Ровно эта неоднозначность уже стоила проекту работы:
несколько источников истины для индексов обсуждались при переносе `TableRegistry`.

Раздел `## Configuration` канона к тому же пригоден и как инструкция: он показывает
валидный на вид JSON с `gateway.vector.index.indexes.<name>.dimension`, а такого
ключа в `config.json` нет (0 совпадений), как нет и `signature_table`, на который
ссылаются требования (0 совпадений и в `config.json`, и в `lib/`). Реальное
объявление устроено иначе: `table`, `pk`, `source_table`, `content_columns`,
`embedding_columns`, `track_column`, `chunk_size`, `chunk_overlap`, `metric`,
`enabled`. То есть пример неверен и по владельцу, и по схеме.

Удаление ломает **два** тестовых файла — и это правильно: тесты должны падать,
показывая, что миграция не завершена, а не подтверждать мёртвое значение.

* `tests/test_config_keys.py` — ассерты на значения удаляемых секций. Диапазоны
  по фактическим блокам — `:90-101` и `:146-159`; взятые по началу, они короче
  блоков: `:88-97` обрывается **внутри** `vector_indexes` и не покрывает `:98-101`,
  а `:146-150` покрывает 5 ассертов из 14 по `gateway.vector.index.*`. Плюс
  комментарий `:114-118` — 15-е совпадение в том же файле, вне обоих диапазонов:
  «Индексы декларируются в `gateway.vector.index.indexes`». Ассертов он не
  утверждает, но это то же живое утверждение об удаляемой секции, поэтому
  вычищается вместе с ними.
* `tests/test_project_settings.py` — тяжелее: `TableEntry` импортируется на
  уровне модуля (`:9-13`), поэтому удаление класса даёт `ImportError` при
  collection и валит **весь файл**, а не один тест. Под нож попадают
  `TestTableEntry` (`:121`), `TestVectorIndexEntryNoSource` (`:389`, локальный
  импорт `VectorIndexEntry` на `:401,412,420`), `TestGatewayVectorIndexConfig`
  (`:446`, валидирует `gateway.vector.index.indexes` и читает
  `result.gateway.vector.index.indexes["audits_index"]` на `:479`) и
  `TestTableEntryTypeLiteral` (`:597`); `:533` упоминает
  `VectorInfrastructureSettings`.

## What this change does

* Переписывает `data/vector-indexes` по факту: владение уходит capability `vectors`
  (`mcp-platform/libs/vectors/`, `platform.json → vectors.indexes`), а требования,
  описывающие `build_vectors`, `PreloadService`, `default_root` и `storage_table`
  как владельца FAISS, удаляются. Прогрев на старте не удаляется: он в коде, и его
  тело переписывает `../2026-10-05-vector-preload-canon/`.
* Вместо удалённых требований вводит одно, которое можно выполнить: состав и
  разбор объявления индексов — на стороне платформы; агент своего списка индексов
  не держит.
* Удаляет мёртвые секции из `config.json` (`gateway.vector.index`,
  `skills.audit_analyzer.tables`, `skills.audit_analyzer.vector_indexes`), их модели
  из `lib/core/project_settings.py` и ассерты из `tests/test_config_keys.py`.
* Запирает результат стражем, причём страж работает на разных механизмах для
  двух секций — различать их обязательно, иначе спека обещает то, чего кода нет:
  * `skills.*.vector_indexes` (и `skills.*.tables`) отвергается **только после
    удаления полей** `SkillSettings.tables`
    (`lib/core/project_settings.py:666`) и `SkillSettings.vector_indexes`
    (`:667`) — сегодня это **объявленные** поля модели, поэтому
    `extra="forbid"` (`:663`) их типизирует, а не отвергает, и обе секции
    сегодня валидируются (`config.json:817,835`). Удаление объявлено в
    `tasks.md` п. 3.3;
  * `gateway.vector.index` отвергается **только после реализации этого change**.
    Сейчас этого механизма нет: `_StrictOptional` — это `extra="allow"`
    (`:55-58`), `GatewaySettings` (`:237`) и `ProjectSettings` (`:759`) своего
    `model_config` не имеют, а единственный отвергатель в ветке `gateway.*` —
    валидатор `_reject_legacy_renamed_sections` (`:253-261`) поверх
    `_LEGACY_GATEWAY_KEYS` (`:782-784`), и он знает только legacy-путь
    `gateway.vector_index` **без точки**. Без явного решения секция будет принята
    как лишний ключ, то есть вернётся ровно тот дефект, который change закрывает.
  * `tests/test_config_keys.py` утверждает отсутствие обеих секций.

## Граница

* **Не трогаем `data/cache-provider`.** Его канон ссылается на те же удалённые
  секции, но закрывается активным change `2026-10-04-close-cache-provider-canon-gap`
  — здесь не дублируем.
* **Не трогаем `COMPONENTS.md`.** Он перечисляет `CacheProvider` и
  `VectorIndexService` как компоненты агента; это зафиксировано в
  `OWNERSHIP.md:92-95` как известное расхождение, отдельный вопрос.
* **Не вводим enforcement `permissions` и не переносим словарь** — см. `Границу`
  в `../2026-10-05-call-boundary-invariants/proposal.md`.
* **Не строим и не прогреваем индексы агентом.** Сборка идёт на старте сервера
  платформы, и это остаётся нормой; спека обязана зафиксировать именно это —
  прогрев возвращается в требования в правильной редакции, а не «до сигнала
  `READY` от `runtime-health` агента».
* **Не публикуем сводку здоровья индексов этим change.** Требование снимается как
  невыполнимое в текущем виде, но сам расчёт в `libs/vectors/preload.py` остаётся и
  не должен быть удалён вместе со спецификацией: возвращать его публикацию —
  отдельное решение, с владельцем и адресатом. Отдельно: то же событие требует
  **второй канон** — `openspec/specs/logging-db/spec.md:689-704` (сценарий
  «preload health-summary через DbLoggingService») предписывает `LogEvent` с
  `event_type="vector_index_preload_health"` и payload `declared` / `loaded` /
  `missing` / `orphan` / `stale`, ссылаясь на реализацию
  `PreloadService.compute_index_health`; `PreloadService` удалён, файла нет. Возврат
  публикации обязан переписать оба канона разом, иначе после архивации два
  документа будут требовать невыполнимое и ни один не скажет, что оно неактуально.
