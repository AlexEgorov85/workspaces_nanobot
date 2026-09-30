# ADR-XXX: Удаление ActiveFilesHook как мёртвого кода

**Дата:** 2026-09-27
**Статус:** Accepted
**Контекст:** OpenSpec change `post-0.3.5-patches-cleanup`

## Контекст

`workspace/hooks/active_files_hook.py` — side-channel для активных файлов сессии
(attachments пользователя + файлы агента). Side-channel через `session.metadata`
с ключами `user_attachments` и `agent_files`.

Side-channel использовался для решения инцидента 2026-08-27, когда
`Consolidator.maybe_consolidate_by_tokens` (в nanobot < 0.3.5) архивировал
сообщения, в результате чего LLM теряла упоминание файлов в system prompt.

## Проблема

После upgrade до `nanobot-ai 0.3.5`:

1. Метод `AgentHook.before_user_turn` **отсутствует** в `nanobot/agent/hook.py:66-151`
   (`nanobot-ai 0.3.5`). Метод `active_files_hook.py:93-119` — мёртвый код,
   никогда не вызывается.

2. Потребителей side-channel-ключей `session.metadata["user_attachments"|"agent_files"]`
   **нет** ни в `lib/`, ни в `workspace/`, ни в `tests/`, ни в `openspec/`,
   ни в `sql/`. Grep по `lib/`, `workspace/`, `tests/`, `openspec/`, `sql/`:
   только сам файл `active_files_hook.py` (10+ упоминаний).

3. Функция `render_active_files_section(...)` (`active_files_hook.py:283`)
   **определена, но нигде не вызывается** (0 упоминаний вне файла).

4. Патч `patch_active_files_in_context`, на который ссылается docstring файла
   (`active_files_hook.py:67, 290`), **физически не реализован**. Grep по всему
   проекту: 2 упоминания (только docstring), 0 определений.

5. `after_execute_tool` (`active_files_hook.py:166-190`) — работает концептуально,
   но `session.save()` не вызывается (`self._sessions = None` в auto-scan через
   `lib/cli/hook_loader.py:75` — `cls(workspace_dir=...)`), поэтому изменения
   `session.metadata` не персистятся.

## Решение

Удалить `workspace/hooks/active_files_hook.py` целиком (370 строк).
Вместе с ним удалить упоминания в:

* `docs/ARCHITECTURE.md:1544`
* `docs/architecture/nanobot-inventory.json:893`
* `openspec/changes/nanobot-035-upgrade/proposal.md:44`

`lib/cli/hook_loader.py::scan_and_register` переводится на allowlist с явным
списком плагинов. Удаление плагина из allowlist защищает от регрессии —
тест `tests/test_active_files_hook.py::test_file_does_not_exist` падает,
если кто-то случайно восстановит файл.

## Альтернативы

| Альтернатива | Почему отклонена |
|---|---|
| Переименовать `before_user_turn` → `before_iteration` | Семантически неверно: вызывается на каждой LLM-итерации (tool-calls, retries), не на user-turn. Дублирование записей. |
| Реализовать через `bus.subscribe(UserInputAccepted)` | +80-100 строк, scope выходит за пределы cleanup. Отдельный change при реальной потребности. |
| Реализовать через `patch_active_files_in_context` monkey-patch | `ContextBuilder` — приватный API; хрупко для 0.4.x; не использует pub-sub. |
| Расширить `OutboundMessage.media` для in-place attach | Покрывает только `agent_files`. Не решает проблему архивации через Consolidator. |

## Нерешённая проблема

Инцидент 2026-08-27 (Consolidator archives PDF references в nanobot < 0.3.5)
**остаётся открытым** после этого change. Решение требует либо:
* Внешний патч upstream `nanobot.Consolidator.maybe_consolidate_by_tokens`,
  чтобы он включал в архив блок с активными файлами;
* Или side-channel **вне session.metadata** (например, persistent файл
  `data_store/cache/active_files/<session_key>.json`, читаемый `ContextBuilder`).

Это **отдельный change при появлении требований**.

## Последствия

Положительные:

* −370 строк мёртвого кода;
* Чистая картина для следующих maintainer'ов — никаких ложных
  ссылок на `patch_active_files_in_context`;
* Нет риска инцидента «side-channel используется, но никем не
  читается» (regression).

Отрицательные:

* Инцидент 2026-08-27 не закрыт. Если он воспроизведётся в 0.3.5
  (Consolidator не активен по умолчанию в 0.3.5, см.
  `openspec/changes/nanobot-035-upgrade/design.md`), нужно отдельное
  решение.
