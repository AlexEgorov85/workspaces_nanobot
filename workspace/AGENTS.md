# Agent Instructions

## Нет оболочки

У агента нет оболочки: `exec`, `exec_session`, `list_exec_sessions` и `run_cli_app`
отключены в `config.json → tools`, поэтому ни шелл, ни скрипт навыка, ни CLI-приложение
вызвать нечем. Вся работа — инструментами агента.

Граница файловых инструментов здесь намеренно **не поднимается**
(`tools.restrictToWorkspace = false`): настройка опиралась бы на
`agents.defaults.workspace` = `~/.nanobot/workspace`, а это подкаталог репозитория, куда
не входят `lib/`, `config.py`, `mcp-platform/`, `tests/`, `docs/`, `sql/` — код, который
агент обслуживает. Разделение держится на трёх вещах: перенаправление (создание файла →
`files/` сессии), отказ (нет резолвера → нет записи) и отключённый `exec` (нет обхода
в обход инструментов).

## File Storage Policy

New files are created inside the `files/` directory of **your own session**, and
always by a **relative** path (`report.csv`, `report/2026/<quarter>.md`). The relative
structure is preserved: `lib/<module>.py` lands in `files/lib/<module>.py`.
There is no tool that creates a file outside `files/` — do not try to compose one.

Creating and editing are different operations. `edit` of an existing project file
(HEARTBEAT.md, MEMORY.md, `lib/...`, etc.) does not change its path: it is work on
the repository, not a session file.

If the session directory cannot be obtained (the platform is declared but does not
answer), the write is **refused with a named reason** — it is never written
"somewhere else". Do not retry the same path: report the reason.
Do NOT try to reach a file by an absolute path of your own machine: it does not exist
here. Work with relative paths.

## Dependencies & pip install

Все библиотеки, которые агенту могут понадобиться для офисных файлов,
веб-запросов, работы с БД и т.п., уже перечислены в `requirements.txt`
корня репозитория и **установлены в venv на сервере**:

- офисные форматы: `python-docx`, `openpyxl`, `xlrd`, `pypdf`,
  `pdfplumber`, `python-pptx`, `Pillow`, `chardet`;
- инфраструктура: `psycopg2-binary`, `httpx`, `loguru`, `PyYAML`, `mcp`,
  `nanobot-ai`. Тяжёлых пакетов (`duckdb`, `faiss-cpu`, `numpy`, `pyarrow`,
  `redis`, `sqlglot`) в корневом `requirements.txt` **нет**: они живут в
  `mcp-platform/requirements.txt` и в `.venv` платформы. Ориентируйся на то, что
  реально лежит в `requirements.txt`, а не на этот список — список протухает
  отдельно от него.

**Установить пакет агент не может** — оболочки нет, а установка на лету всё равно была
бы неверным шагом: если пакета нет в `requirements.txt`, это запрос на расширение
зависимостей. Сообщи пользователю, что нужен новый пакет.

## Absolute paths & hooks

LLM-агенты иногда генерируют абсолютные пути вида
`/home/<user>/<project>/workspace/test/test.md` или
`C:\Users\<user>\workspace\test\test.md`, повторяющие раскладку рабочей
машины, на которой готовился промпт. На другом хосте файл по этому пути
**не существует** (или лежит в недоступной NFS-шаре), и тогда
`lib.utils.media.serialize` не находит вложение → `Media file not found,
keeping path` → в БД уходит AW-dict с пустым `mime_type`/`file_size`.

Чтобы этого избежать:

- **Всегда отдавай относительные пути** в `write_file`/`write`/`create_file` —
  `test/<file>.md`, `report.csv`, `workspace/skills/...`. `SessionFileRedirectHook`
  (см. `workspace/hooks/session_file_redirect_hook.py`) сам перенаправит их в
  `files/` каталога сессии, сохранив структуру исходного пути:
  `lib/<module>.py` → `files/lib/<module>.py`. У `edit` путь, наоборот, не
  меняется — это правка файла проекта.
- В `message({"media": [...]})` тоже передавай относительные пути —
  `SessionFileRedirectHook` сам найдёт файл в `files/` текущей сессии
  (а также в `files/attachments/` и `files/results/` — каталоги сессии, в дереве репозитория они отсутствуют) по относительному пути и
  по имени файла и подставит реальный путь. НЕ придумывай абсолютные пути
  вида `/home/<user>/<project>/workspace/<file>` — на сервере их нет.
  Для файлов, которых реально не существует (`Path(p).is_file()` ложно)
  даже после перенаправления — не прикладывай.

## Файлы сессии и вложения

Вложение, присланное пользователем, лежит в `files/attachments/` каталога **твоей** сессии (в дереве репозитория он отсутствует).
Читать его нужно относительным путём от `files/` — `attachments/<файл>.pdf`:
`document_read` принимает только такой путь и отказывает абсолютному. Имя каталога
сессии знать не нужно — корень объявляет платформа
(`mcp-platform/platform.json → execution.session_root`), а `SessionFileRedirectHook`
перенаправляет относительные пути сам, в том числе в `message({"media": [...]})`.

## Scheduled Reminders

Before scheduling reminders, check available skills and follow skill guidance first.
Use the built-in `cron` tool to create/list/remove jobs.
Get USER_ID and CHANNEL from the current session (e.g., `8281248569` and `telegram` from `telegram:8281248569`).

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

## Heartbeat Tasks

`HEARTBEAT.md` is checked on the configured heartbeat interval. Use file tools to manage periodic tasks:

- **Add**: `edit_file` to append new tasks
- **Remove**: `edit_file` to delete completed tasks
- **Rewrite**: `write_file` to replace all tasks

When the user asks for a recurring/periodic task, update `HEARTBEAT.md` instead of creating a one-time cron reminder.
