"""Physical Document Model для legal_summarizer.

Это **adapter** над ``workspace.utils.office_files``, а не второй парсер.
Использует office_files как source of truth для извлечения текста;
добавляет только координатную метаинформацию (``page_index``,
``paragraph_index``, ``table_index``, ``ordinal``), которой нет в публичном
API office_files (там текст склеивается ``\\n\\n``).

Подробности — ``workspace/skills/legal_summarizer/ARCHITECTURE.md``
(invariants #1, #2, #3).
"""