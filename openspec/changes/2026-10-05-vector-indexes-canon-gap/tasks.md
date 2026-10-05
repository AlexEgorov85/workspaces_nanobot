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
  5 файлов реализации, отсутствуют все (`vector_index_service.py`,
  `cache_provider.py`, `cache_provider_impl.py`, `preload_service.py`,
  `infra_registration.py`, `build_vectors.py` — 6 из 6).
- [x] 1.3 **[O]** Классифицировать: 5 `REMOVED` (прошлый мир), 3 `MODIFIED`
  (намерение остаётся, владелец изменился), 1 `ADDED` (молчание о настоящем).
- [x] 1.4 **[O]** Проверить, что дыру не закрыл никто: у
  `2026-10-04-close-cache-provider-canon-gap` есть только
  `specs/data/cache-provider/`, `vector-indexes` — нет. Проверено обходом дерева.
- [x] 1.5 **[O]** Сверить на пересечение: соседняя дельта про `cache-provider`
  содержит требование «владелец векторных индексов отделён от владельца снимка»,
  но НЕ объявляет `platform.json → vectors.indexes` и НЕ упоминает
  `gateway.vector`. Дублирования нет; оттуда взят только факт ленивой постройки.
- [x] 1.6 **[O]** Написать дельту: 1 `ADDED`, 3 `MODIFIED`, 5 `REMOVED`.

## 2. Открытое решение — проза канона вне `## Requirements`

- [ ] 2.1 **[O]** `data/vector-indexes` содержит 10 прозуических разделов вне
  `## Requirements`, и все они называют несуществующие файлы:
  `## Forbidden Behavior` (`CacheProvider.search_vector`, legacy-таблица
  `public.agent_vector_index_config`), `## Dependencies`,
  `## Configuration` (пример JSON с `gateway.vector.index.indexes`),
  `## Lifecycle` (шаг «build_vectors.py строит FAISS индексы»),
  `## State` (`VectorIndexService` хранит пути к FAISS-файлам),
  `## Invariants` («Конфигурация читается только из config.json»),
  `## Error Behavior`, `## Consumers` (AuditAnalyzer, LegalSummarizer),
  `## Implementation`, `## Verification`.
  Дельтой они не закрываются — тот же вопрос стоит перед
  `2026-10-04-close-cache-provider-canon-gap` (задача 2.2 в его `tasks.md`).
  **Нужно решение, а не молчание:** расширять формат дельт до разделов либо
  править прозу канона напрямую. Заложено: правка прозы вручную тем же change'ом,
  что и дельта, чтобы канон не остался в промежуточном состоянии.
- [ ] 2.2 **[O]** `## Configuration` в каноне — самый опасный раздел: он
  показывает валидный на вид JSON с `gateway.vector.index.indexes.<name>.dimension`.
  Такого ключа в `config.json` нет (0 совпадений), и `signature_table`, на который
  ссылаются три требования, тоже отсутствует (0 совпадений в `config.json` и в
  `lib/`). Пример неверен дважды — по владельцу и по схеме. Реальное объявление:
  `table`, `pk`, `source_table`, `content_columns`, `embedding_columns`,
  `track_column`, `chunk_size`, `chunk_overlap`, `metric`, `enabled`. После удаления
  секции этот пример станет инструкцией «так объявляйте», которой нельзя
  следовать. Правка обязательна, а не косметическая.

## 3. Удаление мёртвого дубля конфигурации

- [ ] 3.1 **[A]** Удалить `gateway.vector.index` из `config.json` (целиком, включая
  `indexes`, `storage_table`, `default_root`, `backend`, `enable`).
- [ ] 3.2 **[A]** Удалить `skills.audit_analyzer.tables` и
  `skills.audit_analyzer.vector_indexes` из `config.json` — обе секции дублируют
  `platform.json → audit.tables` и `platform.json → vectors.indexes`, потребителей
  в `lib/` нет.
- [ ] 3.3 **[A]** Удалить из `lib/core/project_settings.py`: `VectorIndexConfig`
  (`:481`), `VectorInfrastructureSettings` (`:186`), `TableEntry` (`:418`),
  `VectorIndexEntry` (`:449`) и поле `vector` из контейнера (`:246`).
- [ ] 3.4 **[A]** Проверить `SkillsSettings`: после удаления `tables` и
  `vector_indexes` из `SkillSettings` секция навыка обязана остаться
  валидируемой (`enabled`), иначе `config.json` перестанет проходить разбор.
- [ ] 3.5 **[A]** Обновить `tests/test_config_keys.py`: вместо ассертов со
  значениями (`:88-97, :146-150`) утверждать **отсутствие** секций.
- [ ] 3.6 **[A]** Поправить `AGENTS.md` и `mcp-platform/platform.json`
  (`_about`-пояснение про дубликат для `build_vectors.py`): файл сборки в дереве
  отсутствует, оправдание дубля больше не действует.

## 4. Проверка

- [ ] 4.1 **[A]** `tests/test_config_keys.py` — отдельно
- [ ] 4.2 **[A]** `tests/test_enterprise_mcp_config.py` — отдельно (схема
  `ProjectSettings`, `extra="forbid"`)
- [ ] 4.3 **[O]** `python tools/validate_component_specs.py --strict` — число
  ошибок не должно вырасти относительно baseline (у `cache-provider` baseline = 3,
  все три в `runtime/entrypoints`)
- [ ] 4.4 **[A]** `py_compile` по `lib/core/project_settings.py`
- [ ] 4.5 **[A]** Grep-страж после правки: `gateway.vector.index`,
  `gateway_vector_index_config`, `build_vectors`, `register_vector_storage`,
  `CacheProvider.search_vector` — 0 совпадений в `lib/`, `tools/`, `openspec/specs/`
  (кроме исторического `OWNERSHIP.md` и явно помеченного архива)

## 5. Открытые решения — НЕ в этом change

- [ ] 5.1 **[P]** Сводка здоровья индексов посчитана, но не публикуется нигде
  (`mcp-platform/libs/vectors/preload.py:22,57`; производственных вызовов нет).
  Расчёт НЕ удаляется вместе с требованием. Нужен отдельный change: определить
  адресата (оператор при старте? capability `data` в журнал?) и порог, после
  которого сводка обязана быть видна. **НЕ ПРОВЕРЕНО:** решение по адресату не
  принималось.
- [ ] 5.2 **[O]** `COMPONENTS.md` перечисляет `CacheProvider` и
  `VectorIndexService` как компоненты агента и указывает файлы, которых нет.
  Зафиксировано в `OWNERSHIP.md:92-95` как известное расхождение; отдельный
  вопрос.
- [ ] 5.3 **[A]** `tools/legacy_audit.py:121` перечисляет `gateway.vector_index`
  в списке legacy-ключей. После удаления секции запись в списке либо остаётся
  осмысленной (проверка регрессов), либо становится мёртвой — решение при правке.
- [ ] 5.4 **[P]** `mcp-platform/tests/test_vectors_index_health.py` проверяет
  расчёт, который вызывается только тестами. После 5.1 тест получит
  производственного вызова; до 5.1 он остаётся тестом без потребителя — это
  зафиксировано, чтобы его не приняли за мёртвый и не удалили.
