---
name: office_files
description: Чтение и базовый анализ офисных файлов — docx, xlsx, xls, pdf, pptx, csv, txt. Маршрутизация по расширению, извлечение текста и таблиц.
metadata: {"nanobot":{"emoji":"📎","always":true}}
---

# Office Files

Чтение офисных файлов и базовый анализ таблиц/документов. Запрещено `pip install` — все библиотеки уже в `requirements.txt`.

## Когда использовать

- Пользователь прислал файл `.docx`, `.xlsx`, `.xls`, `.pdf`, `.pptx`, `.csv`, `.txt` (или с неизвестным расширением).
- Нужно извлечь текст, таблицы, свойства документа (автор, дата создания, число страниц/слайдов).
- Нужно ответить на вопрос про содержимое офисного файла.

## Маршрутизация по расширению

| Расширение | Библиотека | Замечания |
|:-----------|:-----------|:----------|
| `.docx` | `python-docx` | параграфы + таблицы |
| `.xlsx` | `openpyxl` | значения ячеек, формулы, листы |
| `.xls` | `xlrd` | только чтение; запись — `xlwt` (не установлено) |
| `.pdf` | `pypdf` + `pdfplumber` | текст + таблицы |
| `.pptx` | `python-pptx` | текст + заметки спикера |
| `.csv` | `csv` (stdlib) | определить кодировку через `chardet` |
| `.txt` | открыть с кодировкой из `chardet` | — |
| без расширения / неизвестно | `mimetypes` (stdlib) → сюда же | — |

## Точка входа

Использовать `workspace.utils.office_files` (см. `utils.py` рядом):

```
from workspace.utils.office_files import extract_text, extract_tables, summarize
text = extract_text(path)        # str с текстом документа
tables = extract_tables(path)    # list[list[list[str]]] — таблицы
info = summarize(path)           # dict: pages/sheets/slides/author/...
```

Тип файла определяется автоматически по расширению (fallback — `mimetypes`).

## Возвращаемые структуры

### `extract_text(path) -> str`

Текст «плоско», без форматирования. Колонтитулы и сноски **включены**.
Для xlsx — конкатенация всех непустых листов; для pdf — все страницы через `\n\n`; для pptx — слайды через `\n\n---\n\n`.

### `extract_tables(path) -> list[list[list[str]]]`

Список таблиц; каждая таблица — двумерный массив строк. Для docx — таблицы из тела документа; для pdf — через `pdfplumber.extract_tables()`.

### `summarize(path) -> dict`

- `format` — str (расширение без точки)
- `size_bytes`
- `pages` — для pdf/docx/pptx (если доступно)
- `sheets` — для xlsx/xls (список имён листов)
- `slides` — для pptx
- `author`, `created`, `modified` — для docx/pdf (через `core_properties` / `pypdf` metadata)
- `preview` — первые ~500 символов текста

## Ограничения и подводные камни

1. **OCR недоступен.** Сканы PDF и картинки без текстового слоя вернут пустую строку. Действие: сообщить пользователю, что файл не содержит извлекаемого текста, и попросить текстовую версию.
2. **`xlrd 2.x`** не читает `.xlsx` — только старый формат `.xls`. Для xlsx использовать `openpyxl`.
3. **Защищённые паролем файлы** — `python-docx`/`openpyxl`/`pypdf` бросят исключение. Действие: вернуть ошибку пользователю, пароль агенту не передавать.
4. **`.doc` (старый бинарный формат)** не поддерживается. Если пришёл — попросить сохранить как `.docx`.
5. **Размер файла** в `extract_text` не ограничен. Для очень больших xlsx/pdf (>100MB) — сначала предупредить пользователя.
6. **Кодировки txt/csv** определяются через `chardet.detect()`; при `confidence < 0.7` — попробовать `utf-8`, иначе `cp1251`/`latin-1`.

## Чтение файлов из чата (Telegram)

Файлы от пользователя сохраняются в `data_store/cache/sessions/<session_key>/`. Перед обработкой:

```python
from pathlib import Path
from workspace.utils.office_files import extract_text

path = Path("data_store/cache/sessions/<session_key>/<file>")
text = extract_text(path)
```

Если `path` не существует — ошибку пробросить пользователю с указанием пути.

## Примеры использования

### Текст из docx

```python
from workspace.utils.office_files import extract_text

text = extract_text("data_store/cache/sessions/.../report.docx")
print(text[:500])
```

### Таблицы из pdf

```python
from workspace.utils.office_files import extract_tables

tables = extract_tables("data_store/cache/sessions/.../statement.pdf")
for i, table in enumerate(tables):
    print(f"--- таблица {i} ({len(table)} строк) ---")
    for row in table[:3]:
        print(row)
```

### Лист xlsx целиком

```python
from workspace.utils.office_files import read_xlsx_sheet

rows = read_xlsx_sheet("data_store/cache/sessions/.../data.xlsx", sheet_name="Sheet1")
for row in rows:
    print(row)
```

### Метаданные pdf

```python
from workspace.utils.office_files import summarize

info = summarize("data_store/cache/sessions/.../contract.pdf")
print(info)
# {'format': 'pdf', 'pages': 12, 'author': '...', 'preview': '...'}
```
