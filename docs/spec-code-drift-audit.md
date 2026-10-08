# Аудит соответствия: спецификации ↔ код ↔ документация

**Дата:** 2026-10-08 · **Ветка/HEAD на момент проверки:** `backup/master-pre-future-work-2026-10-02` @ `0b99d555`
(во время аудита ветка уехала вперёд до `70863388` — выводы по cache-TTL перепроверены после этого коммита).

Документ — **срез состояния**, не нормативный контракт. Правила проекта живут в
[`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) и
[`openspec/specs/architecture/component-model/spec.md`](../openspec/specs/architecture/component-model/spec.md).

---

## 1. Метод и границы

Объём: **22 спеки**, **172 требования**, **388 сценариев** (383 заголовка `#### Scenario:`
и 5 заголовков `#### Сценарий:` — русская форма в `runtime/agent-hooks`), **34 документа**.

Пять участков проверки:

| Участок | Охват | Кто проверял |
|---|---|---|
| G1 runtime ядро | entrypoints, context, error-fallback, agent-hooks (32 треб.) | субагент + ревизия |
| G2 runtime сервисы | runtime-patcher, runtime-events-subscription, startup-schema-validation, logging-db (32) | субагент + ревизия |
| G3 data/storage | cache-provider, vector-indexes, session-hybridization, session-recovery, usage-store (39) | субагент + ревизия |
| G4 tools/skills/upgrade | tools-history-search, legal-summarizer-query, upgrade-compatibility (35) | субагент + ревизия |
| G5/G6 сквозной | component-model, skill-tool-boundary, profiles, test-profile-tables, component-registry, component-spec-validation + `docs/`, `AGENTS.md`, CI, change'ы | владелец |

**Каждое утверждение субагента перепроверено открытием цитируемой строки.** Вывод и адрес
проверялись раздельно: в G3 вывод верен, а адрес `open()` был указан как `duckdb_cache_store.py:432`
(там декоратор `@classmethod`, сама `def` — на 433); в G4 адрес `runtime_patcher.py:3608` —
опечатка, фактически 1608. Обе находки приняты с исправленным адресом.

**Что уже было до этого аудита.** Частичное покрытие, полной сверки не было:

- `tests/test_docs_consistency.py` — 4 узких проверки (запрещённые модули в `AGENTS.md`,
  упоминание CLI в `README.md`, дубли ключей `project.json`, битые относительные markdown-ссылки).
  Идёт в CI через `pytest tests/`.
- `tools/architecture_guard.py` — проходит, `exit 0`.
- `tools/validate_component_specs.py` — **падает, `exit 1`**, вне CI.
- Артефакта сквозной сверки спека↔код в репозитории нет (в `docs/_archive/` только точечные
  аудиты: legal_summarizer, history_search, event logging, OpenSpec-миграция).

---

## 2. Сводка расхождений

| Тяжесть | Количество | Характер |
|---|---|---|
| P0 | 6 | код противоречит нормативной спеке или не запускается |
| P1 | 9 | реестр/структурные инварианты, документация вводит в заблуждение |
| P2 | 7 | точечная неточность формулировок и адресов |

---

## 3. P0 — код против спецификации

### 3.1 `benchmarks/runner.py` вызывает несуществующий метод (Spec 3)

Спека `openspec/specs/runtime/agent-hooks/spec.md:37-45` называет ровно три точки shutdown
и запрещает вызов `close_mcp()` дословно: «ДОЛЖНЫ `await agent.aclose()` … И НЕ ДОЛЖНЫ вызывать
`agent.close_mcp()` — это приведёт к `AttributeError`».

Факт по коду:

| Точка | Состояние |
|---|---|
| `gateway.py:483` | `await ctx.agent.aclose()` — корректно |
| `lib/cli/console_loop.py:466` | вызывает только `agent.stop()`, `aclose()` нет |
| `benchmarks/runner.py:726` | `await ctx.agent.close_mcp()` — **нарушение спеки** |

Интроспекцией установлено: у `nanobot.agent.loop.AgentLoop` метода `close_mcp` **нет**
(есть `aclose`). Вызов падает в `AttributeError` и глушится пустым
`except Exception: pass` (`benchmarks/runner.py:727-728`), поэтому дефект невидим: orderly
shutdown, который показывает код, не происходит.

**Это регрессия поверх зелёного чекбокса.** Архивный change
`2026-09-27-post-0.3.5-hook-migration/tasks.md:20` помечает задачу 3.3 как выполненную
и фиксирует критерий проверки «`grep -n "close_mcp" benchmarks/runner.py` → 0 hits».
Сегодня grep даёт попадание. Аналогично:
- `tasks.md:19` (задача 3.2, `console_loop.py`) — тоже ложно: `aclose()` там отсутствует;
- `tasks.md:22` (задача 3.5, замена `agent.close_mcp = AsyncMock()` на `aclose`) — в
  `tests/test_gateway.py:41` до сих пор `agent.close_mcp = AsyncMock()`, при этом сам
  `gateway.py` вызывает `aclose`; объект — `MagicMock` (`tests/test_gateway.py:39`), поэтому
  тест проходит независимо от того, какой метод реально вызывается;
- `tasks.md:21` (задача 3.4, `tests/test_cli_agent.py`) — в файле нет ни `aclose`, ни `close_mcp`.

То есть критерий «зелёный чекбокс» проверялся в момент написания, а не поддерживается: спустя
время изменился код, а чекбокс остался. Именно такие задачи архивации создают иллюзию покрытия,
которую затем наследуют и `docs/`, и новые change'ы.

### 3.2 Фикстура тестов глотает любой `ConfigurationError`

`tests/conftest.py:66-71` ловит `ConfigurationError` и делает `pass` без разбора причины.
`openspec/specs/configuration/profiles/spec.md:193-197` требует не глушить ошибки, кроме
«уже инициализировано», и пробрасывать всё прочее. Комментарий в коде
(`tests/conftest.py:70`) утверждает именно эту различительную логику, которой нет.

### 3.3 `enable_cron` — расхождение значения по умолчанию

Спека: `openspec/specs/runtime/entrypoints/spec.md:48` — `enable_cron=True`.
Код: `lib/core/application_context.py:107` — `bool(gateways.get("enable_cron", False))`.

### 3.4 Жизненный цикл кэша зависит от `enable_audit`

`openspec/specs/data/cache-provider/spec.md:92` — cache runtime создаётся при наличии
секции `gateway.cache`. Код: `lib/core/application_context.py:359` — весь блок создания
кэша под `if ctx.enable_audit:`. Неархивированный change `cache-architecture-alignment`
(`specs/data/cache-provider/spec.md:3-12`) фиксирует **противоположную** норму и в
комментарии сам признаёт, что это «уточнение существующего requirement #2» — но объявляет
её как `## ADDED Requirements`. При архивировании это даст дубликат требования.
Канон новее дельты — дельту переносить нельзя.

### 3.5 Ссылки на несуществующие классы в каноне и реестре

Класс в коде называется `VectorIndexBuildService` (`lib/services/vector_index_service.py:41`),
а имя `VectorIndexService` осталось в **12 местах** нормативных документов:

- `openspec/specs/COMPONENTS.md:56` — и как имя компонента, и как указатель реализации;
- `openspec/specs/data/vector-indexes/spec.md:40,192,232,259`;
- `openspec/specs/data/cache-provider/spec.md:330,378,391`;
- `openspec/specs/runtime/context/spec.md:40,238,272`;
- `openspec/specs/documentation/component-registry/spec.md:71`;
- `openspec/specs/architecture/component-model/spec.md:43` (список имён компонентов).

Отдельно: `runtime/context/spec.md:272` указывал путь `lib/data/vector_index_service.py`,
которого нет (правильный — `lib/services/vector_index_service.py`).

> Поправка к первой редакции отчёта: путь `lib/data/…` приписывался специ
> `data/vector-indexes` — там путь был корректным, и ошибалось только имя класса.

### 3.6 Документация называет механизм, которого нет

Определений `def patch_project_tools` / `def patch_compact_command` в `lib/` — **0**.
Реально существуют `lib/services/project_tool_loader.py::register_project_tools`
и `lib/services/compaction_event_subscriber.py::CompactionEventSubscriber`,
который кормит канал на upstream-событии `ContextCompactionEvent`.

Устаревшее имя было не в двух строках `AGENTS.md`, а **в 15 местах** живых документов
и исходников, включая шаблон для новых tool'ов:

- `AGENTS.md:42,43`;
- `README.md:193`;
- `docs/INTERNAL_API.md:154,160,203,267`;
- `docs/ARCHITECTURE.md:258,265,469-472,492,676,1372,1590`;
- `lib/services/context_compaction.py:7-8` (docstring модуля);
- `lib/services/runtime_inventory.py:250`;
- `workspace/tools/__init__.py:3`, `workspace/tools/example.py:28`,
  `workspace/tools/legal_summarizer_query.py:3`.

`workspace/tools/example.py` — самый дорогой случай: это reference для автора нового
tool'а, и он отправлял его искать несуществующий метод.

Упоминания в `lib/services/project_tool_loader.py:3,148`,
`lib/services/runtime_patcher.py:14-16` и в `tests/*` — **корректный** audit-trail
(«было раньше, вынесено в …»), их трогать нельзя: гвард
`tests/test_runtime_patcher_no_project_tools_boundary.py` явно допускает docstring-упоминания.

---

## 4. P1 — реестр, структура, документация

### 4.1 Реестр покрывает 10 из 22 спек

`openspec/specs/COMPONENTS.md` содержит 10 ссылок на спеки. Вне реестра остались 12:
`infrastructure/test-profile-tables`, `logging-db`, `runtime/agent-hooks`, `runtime/entrypoints`,
`runtime/error-fallback`, `runtime/runtime-events-subscription`, `skills/legal-summarizer-query`,
`storage/session-hybridization`, `storage/session-recovery`, `storage/usage-store`,
`tools-history-search`, `upgrade-compatibility`.

### 4.2 Реестр содержит ложные отметки о выполненной работе

`openspec/specs/COMPONENTS.md:77-78` помечает `[x]` skill-tool-boundary и profiles
с припиской «миграция на русский». Фактически оба файла используют английские заголовки
(`## Purpose`, `## Responsibility`, `## Boundary`). Русский шаблон есть только у
`runtime/startup-schema-validation` (13 русских заголовков).

### 4.3 Два валидатора, оба постоянно красные, ни один не в CI

| Инструмент | Результат |
|---|---|
| `python tools/validate_component_specs.py` | `exit 1`; 21 из 22 спек не проходят (по 9 разделов), `startup-schema-validation` — 2 |
| `openspec validate --specs --strict` | `exit 1`; **14 passed, 8 failed**; ошибок уровня ERROR нет, только warnings «requirement should contain SHALL or MUST» |

`.github/workflows/ci.yml` выполняет только `ruff`, `pytest tests/ -m "not live and not integration"`
и `pytest tests/contract/`. Ни `validate_component_specs.py`, ни `architecture_guard.py`,
ни `openspec validate` в CI не вызываются.

При этом сам канон признаёт статус «будущий»:
`openspec/specs/architecture/component-model/spec.md:476` — «Валидация структуры: будущий CI-скрипт
`validate_component_specs`», тогда как `docs/README.md:116` уже предлагает читателю выполнить
эту команду как «автоматическую проверку структуры».

### 4.4 Требование о проверке путей не реализовано

`openspec/specs/validation/component-spec-validation/spec.md:57-65` требует проверять, что пути
из `## Реализация` существуют. `tools/validate_component_specs.py:187-205` (`validate_registry`)
колонку «Реализация» отбрасывает (`for name, _, _, _, status in rows`) и проверяет существование
спеки подстановочным glob по имени, а не по указанной ссылке.

### 4.5 Несуществующая миграция в каноне — и в коде

`openspec/specs/configuration/profiles/spec.md:124` утверждает, что test-таблицы применяются
миграцией `sql/migrations/V005__test_profile_tables.sql`. Такого файла нет: `sql/migrations/`
содержит `V005__create_agent_cache_ownership.sql`. Каноническая
`openspec/specs/infrastructure/test-profile-tables/spec.md:4` утверждает обратное — применение
через `tools/apply_test_profile_tables.py`. Неверное имя миграции продублировано в комментариях
четырёх DDL-файлов (`sql/workers/create_public_agent_worker_claims_test.sql:12`,
`sql/channels/create_public_agent_conversation_messages_test.sql:8`,
`sql/session/create_public_agent_session_messages_test.sql:8`,
`sql/session/create_public_agent_session_meta_test.sql:8`).

### 4.6 Раздел «Test-профиль» в `sql/README.md` отсутствует

`openspec/specs/infrastructure/test-profile-tables/spec.md:40` требует раздел в `sql/README.md`.
Файл (`sql/README.md`, 209 строк) перечисляет все 6 DDL в дереве каталога (строки 28-49), но
сам раздел и команда `python tools/apply_test_profile_tables.py` не упомянуты ни разу.

### 4.7 Ссылка в `AGENTS.md` на несуществующую спеку

`AGENTS.md` ссылался на спеку `openspec/specs/runtime/startup-vector-preload-gate`, которой нет:
change `startup-vector-preload-gate` не архивирован (24 задачи из 26 выполнены), норма живёт
в его дельте. Агент, читающий инструкции, получал ссылку в пустоту.

> Поправка к первой редакции отчёта: там же было заявлено, что `AGENTS.md:138` указывает
> `profiles/test.json` вместо `profiles/test.jsonc`. Это была ошибка моего же скрипта:
> регулярка `[A-Za-z0-9_./\-]+(?:\.py|\.md|\.json|…)` съела `profiles/test.jsonc` префиксом
> `profiles/test.json`. В файле указано верно `.jsonc`. Класс дефекта полезно запомнить:
> **искомая ссылка может быть подстрокой существующей** — «файл не найден» по такой проверке
> не означает «файла нет».

### 4.8 `docs/README.md` описывает спеки неверно

`docs/README.md:112-113` — «component-level normative specs **на русском**».
Русский шаблон применён к 1 спеке из 22.

---

## 5. P2 — формулировки и неточности

- `lib/core/project_settings.py:87` — docstring «диапазон `0.1 ≤ value ≤ 60.0`»,
  поле `:93` — `Field(gt=0.0, le=60.0)`. Нижняя граница 0.1 не проверяется.
- `lib/core/agent_factory.py:86-87` — «`cron_service` … нужен CLI-режиму, в gateway
  не подключается»; `lib/core/application_context.py:404` — `if ctx.enable_cron and ctx.role == "gateway"`.
  Docstring противоречит коду (и противоречит значению по умолчанию из 3.3).
- `docs/INTERNAL_API.md:340` — «Прямого PostgreSQL-бэкенда у CLI нет».
  `lib/core/application_context.py:1689-1694` создаёт `CacheOwnershipCoordinator(dsn=dsn)`
  и вызывает `try_claim()`; блок не разделён по роли, а `enable_audit` по умолчанию включён.
- `workspace/tools/history_search_tool.py:103-112` — enum из 8 значений.
  `openspec/specs/tools-history-search/spec.md:460-462` перечисляет **7** — в спеке нет
  `turn_completed`, который есть в коде. Ни в спеке, ни в коде нет `turn_failed`, который
  пишет `lib/services/runtime_patcher.py` (строки 362, 1608, 1739, 1742, 1776), — то есть
  реально пишущийся тип событий не достижим через `history_search`. Спека при этом требует
  (`spec.md:464-465`, сценарий `:490-494`) явной ошибки валидации для значения вне списка.
- `tools-history-search` содержит 22 требования и 42 сценария — 8 требований циклически
  ссылаются на предыдущие; `cleanup` не описан ни в одном из них (пробел покрытия).

### Проверенное и признанное корректным

- `lib/services/runtime_inventory.py:176-184` строит канонический список патчей прямо из
  `RuntimePatcher.patch_specs()` — расхождение исключено конструктивно.
- `_PATCH_SPECS` содержит ровно **12** записей; утверждение `AGENTS.md:23` («12 patches») и
  спека `runtime/runtime-patcher/spec.md:118` верны.
- `tools/apply_test_profile_tables.py:73-91` выполняет все 6 скриптов в одной транзакции
  с откатом при ошибке — требование идемпотентности выполнено.
- PG-DDL test-таблиц в `lib/` и `workspace/` отсутствует (найденные `CREATE TABLE` —
  только DuckDB-уровень и фикстуры skill-тестов) — запрет соблюдён.
- Дельта `cache-snapshot-reuse-ttl` (коммит `70863388`) не дублирует канонические требования —
  проверил все 4 заголовка против 10 канонических.

**Отклонённая находка субагента.** G4 утверждал, что `history_search_tool.py:687` редиректит
stderr в `/dev/null` и потому падает на Windows. Проверка не подтвердила ни находку, ни адрес:
файл содержит 603 строки, а подстрок `/dev/null`, `devnull`, `stderr` в нём нет. Это не смещение
номера строки, а несуществующее утверждение — такие находки нельзя принимать по правдоподобию,
поэтому каждая цитата и открывалась.

---

## 6. Рекомендуемый порядок

1. **Блокер:** `benchmarks/runner.py:726` → `aclose()` вместо `close_mcp()`, убрать пустой `except`.
2. **Блокер:** `tests/conftest.py:69-71` → различать «уже инициализировано» и остальные `ConfigurationError`.
3. **Целостность канона:** `enable_cron` (3.3), `cache`↔`enable_audit` (3.4) — выбрать сторону
   и синхронизировать спеку, код и дельту; дельту `cache-architecture-alignment` не переносить
   целиком (`--skip-specs`).
4. **Ложные ссылки:** `AGENTS.md:42-43`, `:138`, ссылка на несуществующую спеку в разделе
   про StartupGate; `COMPONENTS.md:56`; `vector-indexes:272`.
5. **Миграция V005:** убрать несуществующее имя из спеки и из комментариев 4 DDL-файлов.
6. **Реестр:** внести 12 незарегистрированных спек; перевести отметки Wave 1 в честные.
7. **Валидаторы:** собрать один источник истины (структурный) и добавить в CI; решение по
   8 спекам, падающим на SHALL/MUST, принять явно — сейчас они тихо красные.

---

## 7. Поправки к собственным предварительным числам

Числа, названные по ходу работы до пересчёта, и их исправления — чтобы в отчёт не попали
гипотезы:

- «У `runtime/agent-hooks` 0 сценариев» → **неверно**: 5 сценариев в форме `#### Сценарий:`;
  первый пересчёт искал только `#### Scenario:`.
- «Открытых change'ов 3» → **неверно**: 4 (`cache-architecture-alignment`, `cache-snapshot-reuse-ttl`,
  `repeat-guard-hook`, `startup-vector-preload-gate`); первый листинг вернул пустой вывод.
- «В реестре 12 спек» → **неверно**: 10; регулярка жадно захватывала текст соседней ячейки.