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

Три требования спеки при этом не выполнены нигде, и это не «документ отстал»:

* **Требует отдельной команды `build_vectors`** для сбора индекса. Сборка в
  платформе ленивая и автоматическая по первому обращению
  (`mcp-platform/libs/vectors/owner.py:8-11,305-311`).
* **Требует прогрева ДО сигнала `READY`**, запрет запроса без прогрева и публикацию
  сводки в `stderr` и журнал `agent_gateway_logs`. Сводка посчитана
  (`mcp-platform/libs/vectors/preload.py:22,57`) и экспортирована
  (`libs/vectors/__init__.py:47,66`), но **производственного вызова нет ни одного** —
  только `mcp-platform/tests/test_vectors_index_health.py`. Порог «более 30% с
  устаревшими индексами» в журнале не пишет никто.
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
* `skills.audit_analyzer.*` читает только `tests/test_config_keys.py:88-97`;
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

Удаление сломает `tests/test_config_keys.py` — и это правильно: тест должен падать,
показывая, что миграция не завершена, а не подтверждать мёртвое значение.

## What this change does

* Переписывает `data/vector-indexes` по факту: владение уходит capability `vectors`
  (`mcp-platform/libs/vectors/`, `platform.json → vectors.indexes`), а требования,
  описывающие `build_vectors`, отдельный прогрев до `READY`, `PreloadService`,
  `default_root` и `storage_table` как владельца FAISS, удаляются.
* Вместо удалённых требований вводит одно, которое можно выполнить: состав и
  разбор объявления индексов — на стороне платформы; агент своего списка индексов
  не держит.
* Удаляет мёртвые секции из `config.json` (`gateway.vector.index`,
  `skills.audit_analyzer.tables`, `skills.audit_analyzer.vector_indexes`), их модели
  из `lib/core/project_settings.py` и ассерты из `tests/test_config_keys.py`.
* Запирает результат стражем: `gateway.vector.index` и `skills.*.vector_indexes`
  больше не проходят валидацию `config.json` (`extra="forbid"`), и `tests/test_config_keys.py`
  утверждает их отсутствие.

## Граница

* **Не трогаем `data/cache-provider`.** Его канон ссылается на те же удалённые
  секции, но закрывается активным change `2026-10-04-close-cache-provider-canon-gap`
  — здесь не дублируем.
* **Не трогаем `COMPONENTS.md`.** Он перечисляет `CacheProvider` и
  `VectorIndexService` как компоненты агента; это зафиксировано в
  `OWNERSHIP.md:92-95` как известное расхождение, отдельный вопрос.
* **Не вводим enforcement `permissions` и не переносим словарь** — см. `Границу`
  в `../2026-10-05-call-boundary-invariants/proposal.md`.
* **Не строим и не прогреваем индексы агентом.** Сборка остаётся ленивой на
  стороне платформы; спека обязана это зафиксировать, чтобы «прогрев до READY»
  не вернулся в требованиях.
* **Не публикуем сводку здоровья индексов этим change.** Требование снимается как
  невыполнимое в текущем виде, но сам расчёт в `libs/vectors/preload.py` остаётся и
  не должен быть удалён вместе со спецификацией: возвращать его публикацию —
  отдельное решение, с владельцем и адресатом.
