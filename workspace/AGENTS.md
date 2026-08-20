# Agent Instructions

## File Storage Policy

New files must be saved under `data_store/cache/` (e.g. `data_store/cache/report.csv`).
Editing existing files (HEARTBEAT.md, MEMORY.md, etc.) works as before.
Do NOT use shell redirects (`>`, `>>`) in `exec` to create files — use `write_file` instead.

## Dependencies & pip install

Все библиотеки, которые агенту могут понадобиться для офисных файлов,
веб-запросов, работы с БД и т.п., уже перечислены в `requirements.txt`
корня репозитория и **установлены в venv на сервере**:

- офисные форматы: `python-docx`, `openpyxl`, `xlrd`, `pypdf`,
  `pdfplumber`, `python-pptx`, `Pillow`, `chardet`;
- инфраструктура: `psycopg2-binary`, `duckdb`, `faiss-cpu`, `numpy`,
  `pyarrow`, `redis`, `httpx`, `loguru`, `PyYAML`, `streamlit`, `nanobot`.

**Запрещено вызывать `pip install ...` в `exec`.** Причины:

1. `nanobot` shell-tool режет вызовы `pip install -i <url>` через
   SSRF-guard (зеркало PyPI считается «internal/private URL»), и установка
   отвалится без полезного сообщения.
2. Установка пакетов в user-site агенту недоступна, в системный Python —
   тем более.
3. Если пакета нет в `requirements.txt` — это запрос на расширение
   зависимостей, а не повод ставить его на лету. Сообщи пользователю,
   что нужен новый пакет.

Если нужно проверить, что пакет доступен, — используй короткий
`python3 -c "import <pkg>"`; если импорт не падает — пакет есть.

## Absolute paths & hooks

LLM-агенты иногда генерируют абсолютные пути вида
`/home/<user>/<project>/workspace/test/test.md` или
`C:\Users\<user>\workspace\test\test.md`, повторяющие раскладку рабочей
машины, на которой готовился промпт. На другом хосте файл по этому пути
**не существует** (или лежит в недоступной NFS-шаре), и тогда
`utils.media.serialize` не находит вложение → `Media file not found,
keeping path` → в БД уходит AW-dict с пустым `mime_type`/`file_size`.

Чтобы этого избежать:

- **Всегда отдавай относительные пути** в `write_file`/`write`/`edit` —
  `test/test.md`, `data_store/cache/report.csv`, `workspace/skills/...`.
  `SessionFileRedirectHook` (см. `workspace/hooks/session_file_redirect_hook.py`)
  сам перенаправит их в `data_store/cache/sessions/<session_key>/...`.
- Если skill требует указать абсолютный путь — сначала вызови
  `p = Path("...").resolve()` и **не передавай** в `write_file`:
  пусть хук перепишет путь, а не файловая система.
- В `message({"media": [...]})` тоже передавай относительные пути —
  `SessionFileRedirectHook` сам найдёт файл в текущей session-папке
  (`data_store/cache/sessions/<session_key>/`) по относительному пути и
  по имени файла и подставит реальный путь. НЕ придумывай абсолютные пути
  вида `/home/<user>/<project>/workspace/<file>` — на сервере их нет.
  Для файлов, которых реально не существует (`Path(p).is_file()` ложно)
  даже после перенаправления — не прикладывай.

## Scheduled Reminders

Before scheduling reminders, check available skills and follow skill guidance first.
Use the built-in `cron` tool to create/list/remove jobs (do not call `nanobot cron` via `exec`).
Get USER_ID and CHANNEL from the current session (e.g., the `context.session_key` value like `cli:1`).

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

## Heartbeat Tasks

`HEARTBEAT.md` is checked on the configured heartbeat interval. Use file tools to manage periodic tasks:

- **Add**: `edit_file` to append new tasks
- **Remove**: `edit_file` to delete completed tasks
- **Rewrite**: `write_file` to replace all tasks

When the user asks for a recurring/periodic task, update `HEARTBEAT.md` instead of creating a one-time cron reminder.
