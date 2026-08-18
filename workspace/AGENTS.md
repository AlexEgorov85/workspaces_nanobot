# Agent Instructions

## File Storage Policy

New files must be saved under `data_store/cache/` (e.g. `data_store/cache/report.csv`).
Editing existing files (HEARTBEAT.md, MEMORY.md, etc.) works as before.
Do NOT use shell redirects (`>`, `>>`) in `exec` to create files — use `write_file` instead.

## Delivering Files to the User

When the user asks you to "create a file and attach it to the chat" / "размести
файл в чате" / "прикрепи вложением" / "send the file as an attachment", you MUST
call the `message` tool with the created file path in its `media` parameter — do NOT
just describe the file in the text reply. The `message` tool is the only way to
attach a file to the chat via the PostgresChannel media column.

Rules:

- Call `message` with `content` (the user-facing text) and `media=[<path>]` where
  `<path>` is the path you just created (it lives under `data_store/cache/`).
- Omit `channel` and `chat_id` so the tool uses the current runtime channel/chat
  (PostgresChannel / Streamlit / Telegram — whatever the user is on). Do NOT
  hardcode `channel=telegram` or `chat_id=<digits>` unless you are explicitly asked
  to send a cross-channel message.
- One `message` call per logical attachment group is enough — multiple files go in
  one `media=[...]` list.
- Path handling: paths from `write_file` / `edit_file` are absolute (resolved
  by the workspace). On Linux they might look like
  `/srv/audit_nanobot/workspace/data_store/cache/<file>` — pass them as-is.
- Do NOT fall back to describing the file in plain text. If the `message` tool
  itself fails, say so in the reply and surface the error — never pretend the
  file was attached.

Skeleton of a correct delivery turn:

```
1. (plan) write `/abs/path/data_store/cache/report.docx` via `write_file`
2. (act)   call `message(content="Готово, файл во вложении.",
                       media=["/abs/path/data_store/cache/report.docx"])`
3. (done)  the PostgresChannel picks up the media and shows it as a download
           in the Streamlit chat (and as a Telegram document, etc.)
```

### Защита от забывчивости (auto-attach)

Если ты вызвал `message(content="...")` БЕЗ `media`, а в текущем
обороте записывал файлы через `write_file` / `edit_file` / `exec` —
`patch_message_tool` (`RuntimePatcher.patch_message_tool`) автоматически
подмешает свежие файлы в `media`. Это ЛЕЧЕНИЕ, а не костыль: tool
`message` сам знает, что при ответе в текущий чат нужно прикрепить
свежие файлы. Раньше LLM забывал про media-параметр (потому что
описание tool "message" в nanobot 0.3.0 запрещает использовать его
для normal reply в текущем чате), и файлы терялись.

Так что:
- **Всегда вызывай `message` для отправки файлов** — это правильно.
- Если забыл `media` — патч подстрахует.
- Если бот вообще не вызвал `message` (только текст-ответ) — есть
  fallback в `patch_assemble_outbound`, который дополнит media
  свежими файлами перед записью в `agent_conversation_messages`.

Файл **создаётся** в `data_store/cache/` (см. File Storage Policy
выше). Запиши путь в первой строке размышлений — тогда ты не
потеряешь его между итерациями.

## Scheduled Reminders

Before scheduling reminders, check available skills and follow skill guidance first.
Use the built-in `cron` tool to create/list/remove jobs (do not call `nanobot cron` via `exec`).
Get USER_ID and CHANNEL from the current session (e.g., `8281248569` and `telegram` from `telegram:8281248569`).

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

## Heartbeat Tasks

`HEARTBEAT.md` is checked on the configured heartbeat interval. Use file tools to manage periodic tasks:

- **Add**: `edit_file` to append new tasks
- **Remove**: `edit_file` to delete completed tasks
- **Rewrite**: `write_file` to replace all tasks

When the user asks for a recurring/periodic task, update `HEARTBEAT.md` instead of creating a one-time cron reminder.
