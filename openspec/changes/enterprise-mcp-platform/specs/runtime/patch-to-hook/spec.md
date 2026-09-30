## ADDED Requirements

### Requirement: Хуки и события вместо monkey-patch

Поведение, требующее патча внутренностей Nanobot, SHALL сначала проверяться на
реализуемость через `AgentHook`, `EventSink` или собственный класс платформы.
Прямого запрета на хуки нет: хук может наблюдать, писать в БД, публиковать
события в шину хода и менять `turn_context.metadata`.

**Единственное, чего хук не может** — заменить значение, которое фреймворк
передаёт дальше, не используя возвращаемое значение хука. В `0.3.5` такой
случай ровно один. Канонический пример — `nanobot/agent/tools/execution.py`:

```python
result = await tool.execute(**params)
await hook.after_execute_tool(context, tool_call, tool, params, result)
return result, {...}          # возвращается ИСХОДНЫЙ result
```

`after_execute_tool` возвращает `None`, и раннер отдаёт `result` без изменений.
Поэтому хук пригоден для выгрузки результата в файл (побочный эффект), но
**не пригоден для подстановки короткой ссылки в контекст**.

> Проверять следует не наличие хука, а может ли он повлиять именно на то, что
> нужно изменить: побочным эффектом, публикацией события, мутацией `metadata`
> или возвращаемым значением. Возвращаемое значение требуется только для
> подстановки значения.

#### Scenario: Подмена значения недоступна хуку

- **WHEN** требуется заменить содержимое результата tool'а, попадающее в контекст
- **THEN** реализация через `after_execute_tool` MUST быть отклонена с
  указанием на `return result`
- **AND** подстановка SHALL выполняться либо в самом инструменте, либо
  возвращаемым значением хука, либо патчем с явной записью об этом ограничении
  в инвентаре

#### Scenario: Данные передаются событием, а не патчем

- **WHEN** per-turn данные нужно доставить потребителю, который не имеет доступа
  к `OutboundMessage.metadata`
- **THEN** хук SHALL публиковать их через `turn_context.events.publish(...)`
- **AND** потребитель SHALL подписываться на событие
- **AND** `_final_turn`, `_tool_audit` и `media` SHALL передаваться этим способом,
  а не инъекцией в `metadata`

---

### Requirement: У каждого патча есть условие удаления

`docs/architecture/runtime-patcher-inventory.md` SHALL содержать колонку
«Условие удаления» для каждого из 12 патчей. Патч без заполненного условия
удаления SHALL считаться архитектурным нарушением наравне с патчем без записи
в инвентаре.

Условие удаления SHALL быть конкретным: наблюдаемое свойство целевой
библиотеки, при котором патч перестаёт быть нужен (появление конфигурации,
появление фабрики, перенос функции в собственный класс), а не «пересмотреть».

#### Scenario: Патч без условия

- **WHEN** новый патч добавлен в `_PATCH_SPECS` без строки условия удаления
  в инвентаре
- **THEN** проверка MUST падать

---

### Requirement: Не патчить то, что является нашим кодом

Если патруемая цель — объект собственного класса платформы, патч SHALL NOT
использоваться: поведение реализуется в собственном классе.

Канонические случаи этого правила:
`async_save` патчит `agent.sessions.save`, но это собственный
`PGSessionManager`; `session_content_cleanup` патчит `Session.add_message`,
тогда как чистка NUL — забота PostgreSQL и уже реализована в библиотеке
(`clean_text.py`).

#### Scenario: Цель — собственный класс

- **WHEN** требуется изменить поведение вызова собственного класса
- **THEN** изменение SHALL вноситься в этот класс
- **AND** патч Nanobot SHALL NOT применяться

---

## MODIFIED Requirements

### Requirement: Каталог патчей RuntimePatcher

Каталог из 12 патчей сокращается. Целевое состояние — **4–5 патчей**.

| # | Патч | Решение |
|---|---|---|
| 10 | `turn_delivery_fail` | хуки `finalize_content` + `on_error` + `TurnCompleted` |
| 2 | `save_turn` | хук `after_execute_tool` |
| 6 | `assemble_outbound` | события: `_final_turn` → `TurnEndEvent`, `_tool_audit` и `media` → публикация из хука |
| 7 | `async_save` | переносится в `PGSessionManager` |
| 11 | `session_content_cleanup` | переносится в `PGSessionManager.save` |
| 1 | `context_governor` | выгрузка в файл уходит в хук; остаётся только подстановка ссылки для встроенных tool'ов, и она исчезает вместе с переносом тяжёлых запросов в MCP |
| 8 | `session_dir_watch` | удаляется |
| 12 | `document_text_threshold` | уходит вместе с документами |
| 9 | `subagent_logging` | остаётся: у `SubagentManager` нет фабрики хуков |
| 3, 5 | `exec_limits`, `tool_limits` | остаются: конфигурации в 0.3.5 нет |
| 4 | `exec_timeout_cap` | пересматривается: legal уезжает в MCP |

`subagent_logging` — единственный случай, где блокер **структурный**, а не
семантический: точка вставки отсутствует физически, а не потому, что хук
слабее патча.

#### Scenario: Fallback-ответ при ошибке хода

- **WHEN** `_process_message` завершился исключением
- **THEN** пользователь SHALL получить ровно один ответ — текст из конфигурации
  `gateway.error_messages.internal_error`
- **AND** хардкоженный текст upstream SHALL NOT доходить до пользователя
- **AND** событие `turn_failed` SHALL быть записано с `exception_type`,
  `exception_message`, `sender_id`, `agent_id`, `session_key`

#### Scenario: Архив результата tool'а до усечения

- **WHEN** tool вернул результат, превышающий порог сохранения
- **THEN** результат SHALL быть выгружен в
  `workspace/data_store/cache/sessions/<session_key>/` в момент возврата tool'а
- **AND** в контекст SHALL попасть короткая ссылка
- **AND** усечение внутри `_save_turn` SHALL NOT приводить к потере медиа-ссылок

#### Scenario: Каталог и runtime синхронны

- **WHEN** список патчей меняется
- **THEN** `_PATCH_SPECS`, `apply_all()`, `canonical_runtime_patches()` и
  инвентарь SHALL совпадать точно
- **AND** `tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`
  SHALL проходить
