# Tasks

## Легенда

- **[A]** — агент (`C:\Users\Алексей\.nanobot`, дерево `lib/`, `config.json`).
- **[P]** — платформа (`mcp-platform/`, capability `data` / `vectors`).
- **[O]** — владелец канона / решение по спецификациям.
- **НЕ ПРОВЕРЕНО** — утверждение не подтверждено запуском кода в этом change.
  Все ссылки на код ниже получены **поиском по сырым байтам** (PowerShell
  `Select-String`, `rg` через grep-инструмент), а не исполнением.

## 1. Спецификация (этот change)

- [x] 1.1 **[O]** Разобрать `openspec/specs/data/cache-provider/spec.md` по
  требованиям и сверить каждое с кодом. 10 требований, таблица в `proposal.md`.
  Проверено поиском, не запуском кода.
- [x] 1.2 **[O]** Классифицировать расхождения на два класса: прошлый мир
  (`MODIFIED`/`REMOVED`) и молчание о настоящем (`ADDED`). 7 + 1 против 4.
- [x] 1.3 **[O]** Проверить, что дыру не закрыл никто: у
  `2026-10-02-drop-local-cache-read-from-pg` и `2026-10-02-fix-cache-process-boundary`
  **нет каталога `specs/`**. Подтверждено обходом дерева — оба содержат только
  `.openspec.yaml`, `design.md`, `proposal.md`, `tasks.md`.
- [x] 1.4 **[O]** Сверить `unify-runtime-channels` на пересечение: его дельта
  содержит `runtime/{cli-client,context,entrypoints}`, `data/cache-provider` —
  **нет**. Пересечения по требованиям нет.
- [x] 1.5 **[O]** Написать `specs/data/cache-provider/spec.md`: 4 `ADDED`,
  7 `MODIFIED`, 1 `REMOVED`. Заголовки сценариев затронутых требований сохранены
  все; снятые сценарии перечислены явно внутри `REMOVED`.
- [x] 1.6 **[O]** Проверить `openspec validate 2026-10-04-close-cache-provider-canon-gap` —
  зелёный.
- [x] 1.7 **[O]** Проверить, что число ошибок `python tools/validate_component_specs.py --strict`
  не выросло: baseline **3**, после дельты **3** (все три в
  `openspec/specs/runtime/entrypoints/spec.md` — домен `unify-runtime-channels`).

## 2. Открытые решения — НЕ в этом change

- [ ] 2.1 **[P]** NFS-детектор работает только на Linux: на Windows и macOS
  `reject_unsupported_filesystem()` — no-op (`store.py:150`, `:160-161`;
  закреплено тестом `::TestNoFileHold::test_non_linux_is_a_noop`). Требование
  «network/shared filesystem MUST быть отвергнуты» на этой машине не выполняется.
  Нужен отдельный change платформы: определить ФС на Windows/macOS и отвергать
  сетевую, не сужая канон.
  **НЕ ПРОВЕРЕНО:** на Windows-хосте путь действительно не отвергается — вывод сделан
  по коду (`platform.system().lower() not in ("linux", "linux2")` → `return`),
  прогон на сетевом пути не выполнялся.
- [ ] 2.2 **[O]** Прозуические разделы канона вне `## Requirements` дельтой не
  закрываются: `## Public Contract`, `## Dependencies`, `## Implementation`,
  `## Lifecycle`, `## State`, `## Error Behavior`, `## Verification` продолжат
  называть несуществующие файлы и методы после архивации. Нужно решение: расширять
  формат дельт до разделов либо править прозу канона напрямую. Затрагивает
  `architecture/component-model`, не `data/cache-provider`.
- [ ] 2.3 **[O]** `## Error Behavior` канона прямо противоречит коду: «Config
  missing: fail fast при старте (`ConfigurationError`)» против кода, где пустой
  `ENTERPRISE_SNAPSHOT_PATH` даёт поднявшийся сервер и `UnavailableSnapshot`
  (`server.py:484-488`). Правую сторону фиксирует новое требование в этом change;
  старую строку отменить нечем. Входит в объём 2.2.
- [ ] 2.4 **[O]** `COMPONENTS.md` перечисляет `CacheProvider` и `VectorIndexService`
  как компоненты агента с несуществующими файлами. Уже отмечено в
  `openspec/specs/OWNERSHIP.md:92-95`; файл не `openspec/**`.
- [ ] 2.5 **[P]** Текст ошибки `reject_unsupported_filesystem` советует настроить
  `gateway.cache.local_path` (`store.py:193`) — настройки больше нет. Косметика,
  но вводит оператора в заблуждение.

## 3. Проверки, которые осталось сделать — НЕ ПРОВЕРЕНО

Ничего из перечисленного **не выполнялось** в этом change: правки кода не было, а
значит и запускать было нечего.

- [ ] 3.1 **[O]** Прогнать `openspec archive` и убедиться, что канон принял дельту
  без `omits scenario(s) the current spec still has`. Риск снят сохранением
  заголовков, но архив не запускался: он применяет дельту необратимо, а канон
  общий с тремя параллельными писателями.
  **НЕ ПРОВЕРЕНО:** фактический прогон архива.
- [ ] 3.2 **[O]** Отдельно переписать прозу канона (п. 2.2) — до или сразу после
  архивации.
- [ ] 3.3 **[P]** Прогнать тесты платформы, на которые ссылается дельта, чтобы
  подтвердить, что канон описывает живое поведение, а не только текст кода:
  `mcp-platform/tests/test_snapshot_no_file_hold.py`,
  `::test_snapshot_optional_startup.py`, `::test_snapshot_contracts.py`,
  `::test_snapshot_load_service.py`.
  **НЕ ПРОВЕРЕНО:** тесты не запускались. Ссылки взяты по именам тестов, их
  существование подтверждено чтением файлов.
- [ ] 3.4 **[A]** Не требуется: агент к снимку не прикасается, кода в change нет.
