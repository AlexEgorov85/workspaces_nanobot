# Tasks — инвентаризация пула `utils.db` и снос надгробий

**Baseline (зафиксирован ДО первой правки).**

```powershell
npx --no-install openspec validate 2026-10-04-task-queue-channel-switch --strict
# Change '2026-10-04-task-queue-channel-switch' is valid   (exit 0)

python tools/validate_component_specs.py --strict
# Нарушений: 3 — все три в openspec/specs/runtime/entrypoints/spec.md
# (требования без сценариев). Exit 1. Ожидаемая база, чужая область.
```

## 0. Инвентаризация (шаг 3, п. 1)

- [x] 0.1 Перемерить `workspace/utils/db.py`: **1143 строки**, SQL-строк — 8,
      `.execute(`/`executemany(` — 13. Модуль — фабрика пула, не простыня SQL.
- [x] 0.2 Перемерить потребителей: упоминаний `utils.db` — **35 файлов**
      (в постановке было 7). Реальных импортов в продакшн-коде — **4**.
- [x] 0.3 Разложить по категориям (реальный импорт / строковый patch /
      подставной `sys.modules` / docstring) — таблица в `proposal.md`.
- [x] 0.4 Перепроверить остаточный прямой SQL **построчно**: в
      `session_mirror.py` и `project_settings.py` реального SQL нет (совпадения
      — docstring'и), в `lib/` осталось два SELECT'а.

## 1. Шаг 4 — надгробия на платформе

- [x] 1.1 Проверить, что файлы — надгробия. Кода в них нет: только docstring.
- [x] 1.2 Проверить, что операции живут в другом месте. `DataService.claim_task`
      (`service/main.py:1606`) и `DataService.update_task_status`
      (`service/main.py:1634`) на месте; живые операции —
      `tools/claim_task.py`, `tools/update_task_status.py`; реестр —
      `service/registry.py:46,48`.
- [x] 1.3 `git rm` обоих файлов. Выполнено, файлов нет на диске.
- [x] 1.4 Проверить, что удаление — no-op для рантайма. **Проверено запуском:**
      `discover_tool_files` (capability `data`) → **23** файла операций, файлов
      на `_` в выборке загрузчика **ноль**, живые операции на месте. Плюс
      чтением `loader.py:71` и независимым стражем в тестах
      `tests/test_session_mirror_wire.py:67`, который тоже пропускает `_`.

## 2. Шаг 3 — снос `workspace/utils/db.py`: НЕ ВЫПОЛНЕН

- [ ] 2.1 `lib/core/application_context.py` → пул платформы/канала.
      **Заблокировано:** пула-переёмника в агенте нет; альтернатива —
      `schema_check` платформы, но `start()` выполняется до `asyncio.run`
      (`gateway.py:179`, `cli_agent.py:244`), а клиент `async`
      (`enterprise_mcp_client.py:998,1168,1207`). Перенос в живой loop =
      правка `gateway.py` и `cli_agent.py` → **оба вне владения**.
- [ ] 2.2 `lib/services/session_storage.py`. Само по себе безопасно
      (`configure()` только пишет module-global `_dsn`, побочных эффектов нет,
      `workspace/utils/db.py:912-918`; экспорт `os.environ["DATABASE_URL"]`
      независим и остаётся). **Сознательно не выполнено:** `configure(dsn)`
      задаёт пулу DSN из разрешённой секции `channels.postgres`, а
      `resolve_dsn()` без него читает сырой `config.SETTINGS`; равенство этих
      значений не проверено, а снятие `configure` изменило бы, к какому DSN
      подключится живой пул, — риск без пользы.
- [ ] 2.3 Обновить тестовых потребителей. Фактически их не 5, а 8 файлов:
      - `tests/test_utils_db.py` — собственный набор тестов модуля (15 ссылок),
        удаляется вместе с модулем;
      - `tests/test_application_context.py:502,522` — строковый
        `monkeypatch.setattr("utils.db.fetch_with_timeout", …)`;
      - `tests/test_application_context_schema_validation.py:114,132,146,157,172,202`
        — строковый `patch("utils.db.fetch_with_timeout", …)`;
      - `tests/test_startup_schema_validation_live.py:64` — реальный импорт;
      - `tests/test_turn_observability_events.py:146` — реальный импорт.
      Ещё 6 файлов подставляют `sys.modules["utils.db"]`
      (`test_application_context.py`, `test_application_context_logging.py`,
      `test_cli_agent.py`, `test_gateway.py`, `test_runtime_health.py`,
      `test_session_storage.py`) — после удаления станут инертными.
      **Не выполнено** — следствие 2.1.
- [ ] 2.3a **Коллизия владения подтвердилась.** `tests/test_turn_observability_events.py`
      входит и в список потребителей `utils.db` (стр. 146, реальный импорт),
      и в список файлов соседа. На момент начала сессии файл был чист, к
      концу работы уже модифицирован соседом (−220/+18 строк). Правки не
      вносились.
- [ ] 2.4 `git rm workspace/utils/db.py`. **Не выполнено**, пока живут 2.1–2.3.
      Промежуточно: сломаются `gateway.py:653` (отчёт о пуле станет постоянным
      «не удалось подключиться») и `scripts/backfill_media_aw.py:37`.
- [x] 2.5 Зафиксировать нормативное препятствие:
      `openspec/specs/runtime/startup-schema-validation/spec.md:180-187`
      требует пул `utils.db`, `:55-59` запрещает ресурсы вне его.

## 3. Коллизия change'ов (требует решения владельца)

- [x] 3.1 Обнаружено: id `2026-10-04-task-queue-drop-direct-sql` занят
      параллельной работой над `DbLoggingService` (журнал), и она пишет в тот
      же каталог. Обе стороны пришли к одному выводу про надгробия и к одному
      про то, что `utils.db` должен остаться.
- [x] 3.2 Инвентаризация пула вынесена в отдельный change
      `2026-10-04-utils-db-pool-removal`, чтобы два изменения не затирали
      друг друга. Обе стороны сошлись на одинаковых выводах (надгробия
      устарели; `utils.db` должен остаться), но пишут разный объём работ, и
      общий id схлопывал два разных изменения в один каталог.
- [x] 3.3 Опасность снята сама: сосед перезаписал мой `tasks.md` в своём
      каталоге в 23:45, и его change теперь целостен
      (`proposal.md`, `tasks.md`, `specs/data/operation-schema/spec.md`,
      `specs/observability/logging-db/spec.md`). Удалять там больше нечего, и я ничего в
      его каталоге не трогал.

## 4. Итоговые прогоны

- [ ] 4.1 Полный прогон `python -m pytest tests/ -q` — **НЕ ПРОВЕРЕНО**,
      и запускать его не требуется: по стоп-правилу владельца полный набор
      прогоняет он сам в конце волны. Два моих прогона целиком были сняты по
      памяти (на 80% и 28%, без строки итога) — это не регрессия кода, а
      недостаток ресурсов, и перезапускать их не надо.
- [x] 4.1a Пофайловая проверка поверхности, которой касаются мои правки
      (4 файла, один запуск, флаги по стоп-правилу):
      `tests/test_session_mirror_wire.py`,
      `tests/test_platform_import_boundary.py`,
      `tests/test_postgres_channel_claim_contract.py`,
      `tests/test_compaction_observation_wiring.py`
      → **204 passed**, 0 failed, exit 0.
      Это весьмая поверхность: единственный тест, перечисляющий каталог
      операций платформы, — `test_session_mirror_wire.py`, и он пропускает
      имена на `_` (`tests/test_session_mirror_wire.py:67`), как и загрузчик
      (`libs/enterprise_common/loader.py:71`). Правок в `lib/` я не вносил.
      Отдельно подтверждено запуском: `discover_tool_files` на capability
      `data` → 23 файла операций, файлов на `_` — ноль.
- [x] 4.2 `npx --no-install openspec validate 2026-10-04-utils-db-pool-removal --strict`
      — см. отчёт агента.
- [x] 4.3 `python tools/validate_component_specs.py --strict` — **ровно 3**
      нарушения, все в `runtime/entrypoints`; база не изменилась.
- [ ] 4.4 Прогон платформенных тестов — **НЕ ПРОВЕРЕНО** (по условию задачи
      не требовался: удалены два loader-невидимых файла).
