# Tasks — операции платформы модели через штатный MCP (решение А)

**Baseline (зафиксировать ДО первой фазы доступных правок, что считать
сравнением, и не считать baseline).**

```powershell
python -m pytest -q 2>&1 | Select-Object -Last 5
Push-Location mcp-platform; python -m pytest -q 2>&1 | Select-Object -Last 5; Pop-Location
```

> **Правило применимости:** после каждой доступной фазы доступных правок уровень
> не хуже последнего подтверждённого. Каждая фаза доступна только когда
> предыдущая подтверждена.

---

## D1. Проверить осуществимость до правки репозитория

- [x] 1.1 Поднять платформу штатным `MCPProvider` и снять список операций.
- [x] 1.2 Проверить `require_call_meta` в обе стороны: при `false` вызов с
      личностью в аргументах проходит и возвращает её в `_execution`; при `true`
      тот же вызов отвергается `identity_missing`. **Обратная проверка
      обязательна**: тест, зелёный в обоих режимах, не доказывает ничего.
- [x] 1.3 Проверить, что личность действительно не принимается из аргументов
      при `true` (иначе весь план стоял бы на неверном прочтении флага).
- [x] 1.4 Проверить, что минимальный `env` достаточно: платформа достаёт DSN и
      ключи из `mcp-platform/.secrets.env` сама.

## D2. Платформа: открыть переходное окно

- [x] 2.1 `mcp-platform/platform.json → execution.require_call_meta = false`.
- [x] 2.2 Обновить `_about` секции: почему окно открыто и при каком условии
      закроется.
- [x] 2.3 Кода платформы **не трогать**: нужный путь (`_resolve_identity` →
      `_as_meta`) уже написан и покрыт её тестами.

## D3. Объявление платформы

- [x] 3.1 `config.json → tools.mcpServers.enterprise`: stdio, белый список из
      девяти операций, минимальный `env`.
- [x] 3.2 `config._export_platform_process_env`: имя контура и порог журнала
      доезжают до дочернего процесса значениями. **Без `setdefault`**, иначе
      окружение снова станет вторым источником профиля.
- [x] 3.3 Проверить, что `NANOBOT_PROFILE` не воскресает ни в одном виде.

## D4. Хук подстановки личности

- [x] 4.1 `lib/hooks/mcp_identity_hook.py`: подстановка трёх ключей перед
      вызовом `mcp_enterprise_*`.
- [x] 4.2 Присваивание безусловное (не `setdefault`).
- [x] 4.3 ~~При неполной личности ничего не подставлять.~~ **Правило изменено
      2026-10-05, см. 4.6:** прежняя формулировка была шире предмета. Отказ
      остался только для случая, когда нет `session_id` или `user_id` (вызов
      вне оборота вообще). Отсутствие `request_id` — не повод отказывать: на
      неочередном канале (webUI, CLI) вопроса в обороте не существует, и отказ
      делал MCP неработающим на этих каналах целиком.
- [x] 4.4 Проводка в `AgentFactory` последним из фреймворковых хуков и в
      `runtime_inventory` как required.
- [x] 4.5 Гард: `tests/test_mcp_identity_hook.py` — 17 тестов, включая подмену.
- [x] 4.6 Досылка `request_id` на неочередном канале. Правило перенесено в
      `lib/services/turn_identity.py` (владелец личности оборота) как
      `new_envelope_request_id()` (`:168`), и оттуда берут значение оба вызывающих:
      хук импортирует (`:64`) и зовёт (`:221`), клиент фоновых служб импортирует
      и зовёт в `_meta_for` (`enterprise_mcp_client.py:1062`) — приватной копии
      правила у клиента нет. Гард вырос с 17 до **26** тестов (пересчитано, в
      тексте стояло устаревшее «21»): досылка при пустом `request_id` в журнале,
      уникальность значения между двумя вызовами одного оборота, работа без
      сервиса журнала и при его поломке, «сервис журнала только читается», одна
      строка `info` на процесс и отсутствие `WARNING` на пути досылки.
- [x] 4.7 Отказ личности вместо молчаливого пропуска. Хук поднимает подкласс
      типа отказа защитника от повторов (`McpIdentityRefused`, `:84`), поэтому
      существующий патч `repeat_guard_block` превращает его в синтетический
      результат и оборот не падает. Предупреждение «одно на процесс» снято,
      вместо него `ERROR` на **каждый** отказ (`:164`), без прежнего «одна на
      процесс». Гард `TestRefusal` в `tests/test_mcp_identity_hook.py`: отказ
      поднимается, тип отличается от типа отказа защитника, `_reraise` выставлен
      (`:120`), оборот доходит до синтетического результата. Подтверждено
      пересчётом: `TestRefusal` и `McpIdentityRefused` присутствуют, 26 тестов.
      **НЕ ПРОВЕРЕНО:** отдельного теста «чужая ошибка хука не маскируется
      патчем» в `test_mcp_identity_hook.py` найти не удалось — возможно, он живёт
      в `tests/contract/test_repeat_guard_hook_contract.py`, который на эту правку
      не читался; отмечено, чтобы не выдавать проверенное за проверенное.

## D5. Гарды и навыки

- [x] 5.1 `tests/test_mcp_platform_declaration.py`: объявление, флаг, равенство
      имён ключей `LEGACY_IDENTITY_KEYS` файлу платформы.
- [x] 5.2 Снять страж «`mcpServers` обязан быть пуст».
- [x] 5.3 Стражу единого порога журнала объявить третьего читателя и научить
      различать присваивание и чтение.
- [x] 5.4 Переписать навык `audit_analyzer` под штатные операции, добавить
      `enterprise_mcp` с общим контрактом и настоящими кодами отказа.

## D6. Снос трёх обёрток (требует владельца)

- [x] 6.1 Удалить с диска `workspace/tools/audit_analyzer_query.py`,
      `legal_summarizer_query.py`, `history_search_tool.py`
      (`tools/remove_legacy_query_tools.py --apply` — удаление агенту
      заблокировано политикой).
- [x] 6.2 Удалить тесты снесённых инструментов
      (`tools/remove_legacy_query_tests.py --apply`): четыре файла тестов и две
      фикстуры бенчмарка. Покрытие не теряется — оно ушло на платформу
      вместе с кодом: аудит (`test_audit_capability.py` + 12 `test_audit_lib_*`),
      вектора (7 `test_vectors_*`), `history_search`
      (`test_data_service.py::TestHistorySearchIsolation` и
      `::TestHistorySearchFilters`).
      **Бенчмарк сносится не как дубль:** он измерял ILIKE и ORDER BY в
      агентском tool'е. Такого SQL в агенте нет; перенос замера на запрос
      платформы — задача её стороны.

      **Фактически снос уже состоялся** — коммитом `1f22aab` (D6, 2026-10-03),
      тем же, что снёс сами три обёртки: из индекса и с диска ушли все шесть
      целей (`test_audit_analyzer_query_tool.py`, `test_legal_summarizer_query_tool.py`,
      `test_history_search_tool.py`, `test_history_search_benchmark.py` и обе
      фикстуры `tests/fixtures/history_search/`, каталог тоже пуст). Поэтому
      `tools/remove_legacy_query_tests.py --apply` теперь отказывает на первой
      цели («не обычный файл») и ничего не меняет — снос повторно не нужен.

      **Счётчики покрытия в тексте пункта не сходятся с деревом:** на платформе
      шесть `test_audit_lib_*`, а не 12, и восемь `test_vectors_*`, а не семь.
      Само покрытие на месте и собирается: `test_audit_capability.py` + 259 тестов
      `test_audit_lib_*`, а также `test_data_service.py::TestHistorySearchIsolation`
      и `::TestHistorySearchFilters` (8 тестов). Висячих ссылок не осталось —
      ни одного импорта снесённых модулей; упоминания в `tests/` остались
      текстовыми (докстринги, фикстуры логов, отрицательные стражи).
- [x] 6.3 Починить гард-тесты, ссылающиеся на удалённые tool'ы.
      Что оказалось правдой на деле: из 28 файлов, где встречаются имена,
      ломаются девять. Остальные упоминания — фикстуры в тексте или ссылки
      на прошлое.

  - `lib/services/runtime_inventory.py` — из `canonical_project_tools()` убраны
    три `ToolSpec`; канон теперь состоит из `compact_context` и
    `document_read`.
  - `tests/test_history_search_user_isolation_guards.py` — **переписан, а не
    удалён.** Прежний guard грефил удалённый tool' на запрещённые SQL-паттерны.
    Изоляция теперь обеспечена там, где она и живёт: у операции
    `history_search` нет параметра области видимости, а `session_id`/`user_id`
    берутся из `ctx`, а не из аргументов. Это строже прежнего grep'а: раньше
    модель могла попросить `session_scope="all"`, теперь параметра нет вовсе.
  - `tests/test_journal_event_name_alignment.py` — страж перечисления имён
    событий переведён на платформенную операцию. **Что при этом потеряно:**
    модель больше не видит перечень имён и берёт их из самого журнала. Это
    цена перехода, а не дефект; зафиксировано в докстринге класса.
  - `tests/test_runtime_inventory.py` — фикстуры diff'а держали имена снесённых
    инструментов; заменены на живые из канона.
  - `README.md` и `tests/test_docs_consistency.py` — живой раздел README больше
    не предлагает удалённый вход; страж проверяет, что он называет
    `mcp_enterprise_*` и **не** называет снесённых инструментов.
- [x] 6.4 Документация. Правлено: `AGENTS.md` (включая строку 59 — перечень
      `workspace/tools/` называл три снесённых файла), `README.md`,
      `docs/INTERNAL_API.md` (таблица tool'ов + раздел «CLI навыка: режимы»,
      который был вдвойне мёртвым — кроме CLI он описывал снятые
      `build_cache_provider()` и `table_registry.snapshot_path()`),
      `docs/ARCHITECTURE.md`, `docs/skill-tool-inventory.md` (переписан целиком),
      `docs/SKILL_AUTHORING.md` (~18 мест), `docs/skill-tool-architecture.md`
      (§11 заменена на «Контракт доступа к разобранному документу»),
      `docs/TARGET_ARCHITECTURE.md`, `docs/TROUBLESHOOTING.md`,
      `docs/TESTING.md`, `openspec/specs/OWNERSHIP.md`.

  Отдельно, вне перечисления: `docs/ARCHITECTURE.md` содержал **вымышленное
  дерево** — блок `skills/audit_analyzer/scripts/` (12 файлов) и
  `skills/office_files/`, которых в `workspace/skills/` нет уже несколько фаз
  (там остались `SKILL.md` двух навыков), плюс несуществующий
  `workspace/utils/office_files.py`. Сверка шла по фактическому списку файлов,
  а не по правкам: расхождение нашлось потому, что дерево переписывалось по
  памяти.

  `docs/TESTING.md` § «Мёртвые тестовые файлы» врал в **обратную** сторону:
  утверждал, что 12 tombstone-заглушек лежат в репозитории, а они уже снесены
  соседом (`PENDING-DELETIONS.md` § F обновлён, `docs/TESTING.md` — нет).

  **Живые спеки OpenSpec** (решение владельца — переписать, не пометить):
  обе получили дельту в этом change и обновлены в `openspec/specs/`.

  | Спека | Что сделано |
  |---|---|
  | `openspec/specs/skills/legal-summarizer-query/spec.md` | 193 строки, 6 требований → 196 строк, 4 требования. Снят IPC-контракт subprocess'а и wrapper-уровневые коды (`cli_failed`, `cli_not_found`, `subprocess_error`, `empty_response`, `invalid_json`); три требования про manifest переписаны на коды конверта (`not_found` / `internal` / `upstream_unavailable`) |
  | `openspec/specs/interfaces/tools-history-search/spec.md` | 759 строк, 22 требования → 506 строк, 17 требований. Снят параметр `session_scope` и оба его режима; изоляция теперь пересечение `session_id ∧ user_id` из контекста вызова; форма ответа `{hits, next_offset, truncated}`; enum типов событий заменён на пространство имён платформы |

  Ни одно из 22 требований старой спеки не потеряно: `session_scope`-пара
  (2 требования) схлопнута в одно «Scope is the caller session intersected
  with the caller user», `RequestContext exposes user identity` переехала на
  хук, `Формат ответа`/`Детерминированный порядок`/`Пустой результат` — в
  `pagination and ordering` и `response shape`, `history_search is read-only` —
  в `operation identity and scope`.

  **Зафиксировано в спеке как факт, а не как замысел:** опубликованная схема
  `query_operation` строится из сигнатуры обработчика, поэтому `field` не имеет
  `enum`, а `max_chunk_summary_chars` — границ. Проверял это argparse снятой
  обёртки. Следствие: значение `field`, не совпадающее ни с одним из шести,
  молча отдаёт ветку `all` целиком, без отказа. Это дефект платформы, не агента.

  **Не трогать:** `CHANGELOG.md`, `PENDING-DELETIONS.md`,
  `docs/PLAN-SPEC-COMPLETION.md`, `mcp-platform/docs/MIGRATION.md` и
  `mcp-platform/docs/TARGET-ARCHITECTURE.md` — это пересказ прошлого, а не
  описание текущего состояния.

  **Было за рамками (чужие файлы) — сосед закрыл, сверено по диску:**
  - `workspace/TOOLS.md` больше не называет снесённые tool'а как живые: §
    `data.history_search` (стр. 48) и § `platform.query_operation` (стр. 370)
    упоминают их только чтобы сказать, что их нет — «удалена change'ом
    `2026-10-03-mcp-native-tools` (п. D6)» (стр. 50-52), «удалён ещё раньше»
    (стр. 375), «Параметра `session_scope` **нет**» (стр. 79).
  - `mcp-platform/libs/legal_summarizer/skill/SKILL.md` (389 строк): раздела
    «IPC contract for follow-up queries» в файле нет, § «Контракт отказа по
    операции `query_operation`» (стр. 268-276) говорит обратное прежнему —
    «Подпроцесса нет, его stdout не разбирается», а `cli_failed`,
    `cli_not_found`, `subprocess_error`, `empty_response` и `invalid_json`
    «не существуют ни в коде, ни в контракте». § «Что внутри» (стр. 313-325)
    перечисляет настоящие файлы, `scripts/` среди них нет; несуществование
    `workspace/skills/legal_summarizer/scripts/` оговорено отдельно
    (стр. 103-111).
  - Противоречие на стороне платформы снято: в
    `mcp-platform/servers/enterprise/tools/query_operation.py` нет ни
    `AUDIENCE_RUNTIME`, ни `runtime-only`, а `list_scripts.py` уехал в
    `mcp-platform/servers/enterprise/capabilities/audit/tools/list_scripts.py`
    и в строках 13-18 сам фиксирует: «Метка `runtime-only` снята».
    Константа `AUDIENCE_RUNTIME` не «нигде не используется» — она объявлена в
    `mcp-platform/servers/enterprise/capabilities/data/service/main.py:64` и
    используется этим файлом десятками мест.

## D7. Отдельной задачей, не здесь

- [ ] 7.1 Снятие клиента агента и второго процесса: требует переноса
      фоновых служб (`queue_ops`, `session_cold_sync`, `log_transport`,
      `context_compaction`) на иной способ доступа. Они работают вне оборота.
