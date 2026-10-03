# Tasks — session-files

**Baseline (заполняется перед стартом фазы 1, числа вставляются из фактического
прогона — не переносить из других change):**

```powershell
python -m pytest -q 2>&1 | Select-Object -Last 5
Push-Location mcp-platform; python -m pytest -q 2>&1 | Select-Object -Last 5; Pop-Location
```

> **Правило применимости:** после каждой фазы допустимы ровно те же падения, что
> и в baseline. Каждая фаза заканчивается прогоном обоих наборов. Строка
> допустимых падений пересобирается только вместе с фазой, которая эти тесты
> убирает, и не тихо.
>
> **Известные предсуществующие расхождения, которые этот change НЕ чинит:**
> `mcp-platform/.sessions/startup_gateway` на диске; расхождение
> `runtime_inventory.canonical_plugin_hooks()` и
> `lib/cli/hook_loader.py::_allowed_hook_names()` по `StreamDiagnosisHook`
> (канон объявляет хук, allowlist его не содержит).

**Ограничение на каждом шаге:** не переносить несколько фаз в один коммит;
не удалять старое поведение до прохождения контрактного теста; не править
чужие незакоммиченные правки в общих файлах (`docs/ARCHITECTURE.md`,
`lib/channels/postgres_channel.py`, `config.json`).

**Коммиты:** владелец коммитит сам, каждый пункт — самодостаточная правка.
Перед `git add` проверять, что чужая незакоммиченная работа не попала в индекс.

---

## Фаза 0. Стражи до кода

Цель фазы: зафиксировать расхождение **до** того, как начнём его сглаживать.
Без падающих тестов фазы 1–3 нечем проверить.

- **0.1** `tests/contract/test_session_dir_name_contract.py` — таблица из 15
  ключей (не-ASCII, пробел, несколько двоеточий, разделитель пути, `..`, `.`,
  пустая строка, `CON`, 200 символов, точка в конце, `__nosession__`, плюс боевые
  `cli:1` и `telegram:8281248569`). **Четыре** стороны: хук, `SessionFileStore`,
  `enterprise_common.safe_name`, `legal_summarizer.safe_session_key`.
  Импортируются настоящие функции (по файлу для платформы), а не их логика.
  Вторая проверка — изоляция: разные клюги не должны давать один каталог даже
  если все стороны сойдутся на общем неверном правиле.
  **Ожидаемо падает** — расхождение измерено, 11 значений из 15.
- **0.2** `tests/test_no_hardcoded_session_paths.py` — по образцу
  `tests/test_no_hardcoded_table_names.py`. Проверяются **строковые литералы и
  склейки литералов через `/`** в production-коде агента и платформы, плюс
  два конфига. Правило по форме обязательно: прежний корень в хуке собран как
  `self._workspace / "data_store" / "cache" / "sessions"`, и подстрока в одном
  литерале его не видит — без этого правила страж поймал бы описание в
  инвентаре и промолчал бы про само вычисление.
  `docs/` и `.md` из стражей исключены осознанно: проза не пишет файлы, её
  правит фаза 5, а два стража на один файл означают, что виноват всегда чужой
  прогон. **Ожидаемо падает.**
- **0.3** Зафиксировать baseline из блока выше, вписать числа.

### Baseline (измерен 2026-10-03, ветка `refactor/mcp-platform`, HEAD `e134f7b`)

| Набор | Результат | Время |
|---|---|---|
| агентский `pytest -q` | **4 failed**, 3133 passed, 46 skipped, 2 xfailed | 142.66s |
| платформенный `pytest -q` | **1 failed**, 5456 passed, 1 skipped | 88.33s |

Из четырёх падений агентского набора — **все четыре новые стражи фазы 0**:

- `tests/contract/test_session_dir_name_contract.py::test_every_side_agrees_on_every_key`
  (расхождение 11 значений из 15);
- `tests/contract/test_session_dir_name_contract.py::test_distinct_keys_never_share_one_directory`
  (агентская сторона схлопывает `привет`/`ключ`/`重` в один `__nosession__`);
- `tests/test_no_hardcoded_session_paths.py::test_no_hardcoded_session_path_in_code`
  (6 мест);
- `tests/test_no_hardcoded_session_paths.py::test_no_hardcoded_session_path_in_configs`
  (2 места).

То есть **до фазы 0 агентский набор был зелёный** — падать ровно на эти четыре
проверки и не больше.

Падение платформенного набора — **чужое и незавершённое**:
`mcp-platform/tests/test_settings_registry.py::TestCapabilityTree::test_every_setting_belongs_to_a_capability_or_is_shared`
(«настройки без capability и без SHARED_SETTINGS:
`ENTERPRISE_SESSION_MESSAGES_TABLE`, `ENTERPRISE_SESSION_META_TABLE`»). Оно
возникает из-за правки `mcp-platform/libs/enterprise_common/settings.py`,
которая в момент замера была незакоммиченной. К этому change отношения не
имеет; число может сдвинуться, когда сосед допишет свою работу.

Находка фазы 0, которой не было в плане: страж 0.2 нашёл **пятого** писателя —
`mcp-platform/libs/legal_summarizer/cache/document_cache.py:114-125` пишет в
`workspace/data_store/cache/sessions/<key>/documents/`, то есть в дерево
сессий агента, и приносит четвёртую копию `safe_session_key`. Вынесена в
задачу 4.5.
- **0.4** Подтвердить решения D1–D4 у владельца. Если решение отличается от
  умолчания — переписать соответствующее требование в
  `specs/runtime/session-files/spec.md` **до** фазы 1.

**Приёмка фазы:** оба теста падают по ожидаемым причинам (не по ошибке
импорта/фикстуры), baseline записан.

---

## Фаза 1. Платформа: корень, раскладка, операция `session_files`

- **1.1** `mcp-platform/platform.json` → `execution.session_root =
  "${NANOBOT_WORKSPACE}/data_store/sessions"` — **внутри** корня проекта, иначе
  запись в папку сессии отклонит граница инструмента. Переменная
  `NANOBOT_WORKSPACE` экспортируется в `config.py::_export_runtime_env` рядом с
  `NANOBOT_PYTHON` и `NANOBOT_PROJECT_ROOT` (тот же приём, тот же момент
  старта); дополнить `tests/test_enterprise_mcp_config.py`. Подстановка
  разворачивается существующим `_expand` (`settings.py:1350`); отсутствующая
  переменная даёт отказ с именем — это уже так, отдельного кода не нужно.
  **Уточнения по факту:** `config.py` лежит в **корне репозитория**, а не в
  `lib/core/`; значение рабочего каталога — `<корень проекта>/workspace`, тем же
  приёмом, что и `workspace_dir` в `gateway.py` и `cli_agent.py`.
- **1.1a** Убрать молчаливый запасной путь в
  `mcp-platform/servers/enterprise/server.py::_session_root`:
  `settings.get(...) or ".sessions"` — это второе неявное объявление корня,
  ровно тот дефект, который change убирает. Отсутствие объявления → отказ с
  названным ключом. Ключ `ENTERPRISE_EXEC_SESSION_ROOT` при этом **не** является
  вторым объявлением: он зарегистрирован в реестре настроек как
  `file_key="execution.session_root"`, владелец — платформа.
- **1.1b** Подстановку `${NANOBOT_WORKSPACE}` заглушить в окружении
  платформенных тестов: `mcp-platform/tests/conftest.py` прямо требует этого в
  своём комментарии, а `test_enterprise_data_db.py`, `test_pool_settings_seam.py`,
  `test_server_bootstrap.py` держат свои копии словарей настроек. Без этого
  набор платформы падает на **сборе коллекции**.
- **1.2** `libs/enterprise_common/session/workspace.py`: `SESSION_SUBDIRS` +=
  `"files"` первым; доктрина в докстринге модуля — почему `files/` отличается от
  шести платформенных.
- **1.3** `servers/enterprise/tools/session_files.py` — операция уровня
  платформы. Вход: `ensure: bool = true`. Выход: `session_id`, `root`, `files_dir`,
  `layout`, `created`. `session_id` — **из контекста исполнения**, не из
  аргументов и не из `_meta` как транспорта: личность может прийти и через
  `_meta`, и подставленной в аргументы (переходный режим
  `require_call_meta = false`, change `mcp-native-tools`), и операция обязана
  работать при обоих. **Отказ без идентичности делает сама операция:**
  полагаться на общеплатформенный отказ нельзя — в переходном режиме он
  выключен, и операция без защиты создала бы каталог для вызова без сессии.
- **1.4** Регистрация операции в `servers/enterprise/server.py::build` рядом со
  сборкой слоя исполнения — **не** через загрузчик capability-каталогов
  (прямо сказано в `tools/__init__.py`).
- **1.5** `mcp-platform/tests/test_session_files_operation.py`: создаёт папку,
  повторный вызов не пересоздаёт, `ensure=false` не создаёт, вызов без `_meta`
  даёт `identity_missing`, пути в ответе абсолютные.
- **1.6** Обновить `mcp-platform/tests/test_session_workspace.py` под семь
  подкаталогов.
- **1.7** `mcp-platform/docs/MCP-CONTRACTS.md`: контракт операции + `files/` в
  раскладке сессии.

**Приёмка фазы:** `mcp-platform/tests` зелёный; в ответе операции
`<NANOBOT_PROJECT_ROOT>/data_store/sessions/<name>/files`; старый
`mcp-platform/.sessions` больше не создаётся (проверяется живым стартом
сервера, тестом с временным `session_root`).

---

## Фаза 2. Агент: единственный резолвер

- **2.1** `lib/services/session_files.py`: `SessionFileResolver`
  (`session_dir`, `files_dir`, `ensure`, кэш по `session_key`) и
  `SessionFilesUnavailable`. Резолвер берёт каталог у платформы операцией
  `session_files`; без платформы — из объявления агента.
- **2.2** `config.json` → `gateway.agent.session_files.root`. Ключ действует
  **только** при выключенной платформе; это проверяется тестом, а не
  комментарием. Запись рядом в `tests/test_config_keys.py`.
- **2.3** `lib/services/enterprise_mcp_client.py`: `session_files(identity)` —
  один вызов на `session_key`, результат кэшируется; без кэша хук дёргал бы
  платформу на каждый файл.
- **2.4** Подключить резолвер в composition root (`ApplicationContext.create()`,
  рядом с `RuntimePatcher.apply_all()` и `register_project_tools()`) и отдать его
  хуку. Способ — как у `project_tool_loader` (приватно на контексте), контракт
  проверяется тестом в том же духе, что `tests/test_project_tool_ctx_contract.py`.
- **2.5** `tests/test_session_files_resolver.py`: кэш, отказ при недоступной
  платформе, режим без платформы, поведение при смене корня на процессе.
- **2.6** Дополнить `tests/test_enterprise_mcp_settings_contract.py`: клиент
  SHALL NOT передавать корень файлов сессий в args и SHALL NOT добавлять
  переменную корня в `_child_env()`.
- **2.7** `lib/services/runtime_inventory.py`: убрать захардкоженный путь из
  описания `SessionFileRedirectHook` — канон расходился с фактом, и страж 0.2
  его ловил. Записи для самого резолвера **не добавляем**: инвентарь ведёт
  хуки, фабрики хуков, проектные инструменты и патчи, а `SessionFileResolver` —
  служба, и она ни в одну из категорий не относится. Добавлять её туда
  означало бы завести в инвентарь то, чего он не описывает.

**Приёмка фазы:** `tests/test_session_files_resolver.py` зелёный; страж
0.2 больше не видит `data_store/cache/sessions` в `lib/` (сам хук ещё пишет
туда — это закрывает фаза 3, поэтому здесь падение 0.2 допускается, и это
отмечается в строке допустимых падений).

---

## Фаза 3. Хук: запись только в `files/`

- **3.1** `workspace/hooks/session_file_redirect_hook.py`: цель редиректа —
  `files_dir` от резолвера. Сохранять **относительную структуру** пути
  (`files/lib/services/new_module.py`), санитизируя каждый сегмент; коллизия —
  суффиксом, как сейчас.
- **3.2** `edit` не переписывается. `write`/`create_file`/`write_file` — всегда
  в `files/`, без исключений. Из логики создания убрать `data_store/`; белый
  список оставить только для правки существующего файла проекта.
- **3.3** Недоступный резолвер → **отказ** перенаправления с названной
  причиной. Молчаливый возврат к записи «как есть» запрещён: он вернул бы ровно
  тот дефект, который эта фаза убирает.
- **3.4** Поиск вложений для `message`: `files/`, `files/attachments/`,
  `files/results/`. Fallback-поиск по `data_store/cache/` убрать.
- **3.5** Две копии санитайзера — в одну: `workspace/utils/session_file_store.py:27`
  удаляется, `workspace/utils/session_key.py` остаётся единственным
  агентским. Обе должны пройти контракт 0.1 — иначе тест упадёт и покажет,
  где именно разъехалось.
- **3.6** Тесты: переписать `tests/test_session_file_redirect_hook.py` под новые
  правила (создание/правка/whitelist/отказ), поправить
  `tests/test_session_key.py`, проверить `tests/test_recent_files_hook.py`
  (авто-attach не должен сломаться на новых путях).

**Приёмка фазы:** тесты 0.1 и 0.2 зелёные; вручную: `write("lib/foo.py")` даёт
`…/files/lib/foo.py`, `edit` существующего файла проекта его не трогает.

---

## Фаза 4. Канал и вложения

- **4.1** Сначала прочитать `workspace/utils/session_file_store.py` целиком: у
  него своя раскладка (`results/`, `attachments/`), свой `archive/` под кэшем и
  своя очистка по `max_files`/`max_age_hours`. Правка без этого чтения —
  источник второй, не меньшей путаницы.
- **4.2** `lib/channels/postgres_channel.py`: удалить `_resolve_sfs_base` (он
  вычисляет базу из строки конфигурации и уже стоил расхождения
  `…/cache/cache/sessions`), путь брать у резолвера.
- **4.3** Входящие вложения → `files/attachments/` сессии. Если по D2 общий кэш
  остаётся — он уходит в `data_store/shared/`, и это отдельная папка вне дерева
  сессий.
- **4.4** `config.json → channels.postgres.media_cache_dir`: после 4.2 ключ
  без потребителя → в `PENDING-DELETIONS.md`, не удалять молча.
- **4.5** `mcp-platform/libs/legal_summarizer/cache/document_cache.py:114-125` —
  найдено стражем 0.2, а не вручную: кэш документов пишет в
  `<root>/workspace/data_store/cache/sessions/<key>/documents/`, то есть
  **в дерево сессий агента**, минуя `SessionWorkspace`, и приносит с собой
  четвёртую копию `safe_session_key` (`legal_summarizer/cache/session_key.py:33`).
  Либо уходит на платформенную операцию/подкаталог, объявленный платформой,
  либо явно переносится под `files/`. Решение владельца: документы сессии —
  это данные сессии (`files/`) или артефакты платформы (`artifacts/`).
- **4.6** Уборка мёртвых деревьев: `workspace/data_store/cache/cache/`,
  `workspace/data_store/media/cache/`, корневой `data_store/`. Проверить
  `data_store/` на содержимое перед удалением.
- **4.7** Тесты: `tests/test_utils_session_file_store.py`,
  `tests/test_postgres_channel.py`, `tests/test_smoke_postgres_channel_media.py`,
  `tests/test_gateway_live_media_e2e.py`, тесты `legal_summarizer` на кэш.

**Приёмка фазы:** вложение, пришедшее из PostgreSQL, находится в папке сессии и
доступно агенту (тот самый сценарий, который раньше ломался из-за двойного
`cache`).

---

## Фаза 5. Документация, инвентарь, уборщик

- **5.1** Места, где путь сессии записан строкой (обязательно все, иначе
  документация снова разойдётся с кодом):
  `workspace/AGENTS.md`; `AGENTS.md:83`; `docs/ARCHITECTURE.md:1245,1682,1795`;
  `docs/SKILL_AUTHORING.md:571,589,590,595,905`;
  `docs/architecture/runtime-patcher-inventory.md:260`;
  `lib/channels/README.md:35,58`; `lib/services/runtime_inventory.py:105`.
- **5.2** `tools/cleanup_scratch.ps1`: защищённый список (строка 66) —
  `mcp-platform/.sessions` больше не является живой папкой; новый путь
  `data_store/sessions` добавить в защищённые, иначе уборщик снесёт рабочую.
- **5.3** `CHANGELOG.md` — запись о смене раскладки; `PENDING-DELETIONS.md` —
  про `media_cache_dir` и две копии санитайзера.
- **5.4** `docs/audit/_scripts/build_inventory.py` и материалы аудита не
  трогаем: это отчёт о состоянии на дату.

**Приёмка фазы:** страж 0.2 зелёный на всей репозитории; в `docs/` нет
ссылок на старые пути, кроме явно исторических.

---

## Фаза 6. Проверка и приёмка

- **6.1** Полный прогон обоих наборов; сравнение со строкой допустимых падений.
- **6.2** **Прогон в отдельном worktree на закоммиченном HEAD**
  (`git worktree add --detach <tmp> HEAD`) — рабочее дерево содержит чужие
  незакоммиченные правки и показывает результат, которого на ветке нет.
  Уборка: `git worktree remove --force` + `git worktree prune`.
- **6.3** Живая проверка: папка сессии появляется в `data_store/sessions/<key>/`
  с семью подкаталогами; файл, созданный моделью, лежит в `files/`; крупный
  результат платформы — в `results/`; `mcp-platform/.sessions` не растёт.
- **6.4** `tools/diagnose_startup.py` — раскладка хуков не поехала
  (`runtime_inventory` и факт совпадают).

**Приёмка фазы:** оба набора зелёные на закоммиченном HEAD; живая проверка
6.3 выполнена и зафиксирована в коммите фазы.

---

## Фаза 7. Отключение `exec` (последняя операция, D5)

Выключается одним флагом `tools.exec.enable = false` — он гасит `ExecTool`,
`ExecSessionTool` и `ListExecSessionsTool` (общий `config_key = "exec"`).
Порядок пунктов обязателен: **7.1–7.4 — до, 7.5 — после.**

- **7.1** Починить противоречие инструкций. `workspace/TOOLS.md:314-340`
  велит вызывать `audit_analyzer` через `exec`
  (`python workspace/skills/audit_analyzer/scripts/cli.py --mode predefined`),
  а `workspace/skills/audit_analyzer/SKILL.md` говорит обратное: «Инструмент
  `audit_analyzer_query`… Не вызывай `exec` / `python` ради данных аудита».
  Живой контракт — проектный инструмент. Раздел в `TOOLS.md` переписывается
  на `audit_analyzer_query`; расхождение фиксируется тестом или ревью.
- **7.2** Вычистить остальные ссылки на `exec` в инструкциях модели:
  `workspace/AGENTS.md:7,21-24,62-66,89` (раздел «Пути к media-attach при
  вызове CLI skill'ов через `exec»» после отключения бессмыслен — его
  проблема уходит сама), `workspace/TOOLS.md:6,15,19,31,286-288,310`,
  `mcp-platform/libs/legal_summarizer/skill/SKILL.md` (5 вхождений),
  `workspace/USER.md:38`.
- **7.3** `tools.cliApps`: сейчас `enable = true`, установлен `minimax-cli`
  (`cli-apps/installed.json`) — это **второй** путь исполнения кода. Либо
  выключить, либо ограничить каталогом; решение владельца фиксируется в
  `TOOLS.md`, потому что инструмент видит модель.
- **7.4** `document_read._resolve_path` (`workspace/tools/document_read.py:283-299`):
  `Path(path).expanduser()` + `is_file()` — читает **любой** абсолютный путь на
  хосте, ограничение только `SUPPORTED_SUFFIXES`. После отключения `exec` это
  остаётся единственным чтением мимо границы. Ограничить папкой сессии;
  описание `path` в схеме (`:84-89`, «Абсолютный либо относительный к
  workspace») привести в соответствие.
- **7.5** `tools.restrictToWorkspace` **НЕ включать**, и это решение зафиксировать.
  Настройка опирается на `agents.defaults.workspace` = `~/.nanobot/workspace`, а
  это **подкаталог** репозитория: `lib/`, `config.py`, `mcp-platform/`,
  `tests/`, `docs/`, `sql/` в него не входят. Включение отрезало бы агента от
  кода, который он обслуживает. Разделение объёмов держится на
  перенаправлении (создание файла → `files/`), отказе (нет резолвера → нет
  записи) и на отключённом `exec` (нет обхода в обход инструментов). Настройка
  остаётся `false` намеренно, и это должно быть записано в документации, а не
  выглядеть как недосмотр.
- **7.6** Только теперь `tools.exec.enable = false` в `config.json`, отдельным
  коммитом.
- **7.7** Применить дельту `specs/logging-db/spec.md`: применённая спека
  нормативно закрепляет вызов Skill через `tools.exec` (`logging-db/spec.md:482-490`,
  сценарий `:515-528`, перекрёстная ссылка `:1943-1946`) — после отключения
  это описание несуществующего пути. Дельта обобщает формулировку до «вызов
  функциональности Skill инструментом агента».
- **7.7** Живая проверка: в наборе модели нет `exec`/`exec_session`/
  `list_exec_sessions`/`cli_apps`; `audit_analyzer_query`,
  `legal_summarizer_query`, `document_read`, `history_search`, `compact_context`
  на месте; скилл `audit_analyzer` отвечает через инструмент.
- **7.8** Прогон обоих наборов + worktree-прогон 6.2. Ожидаемые падения,
  связанные с `exec` (тесты, проверяющие шелл-вызовы), перечислить здесь же
  явно, а не молча убрать.

**Приёмка фазы:** модель не может ни создать, ни прочитать файл вне папки
сессии ни одним из оставшихся инструментов; набор инструментов в стартовом
логе соответствует `config.json`.
