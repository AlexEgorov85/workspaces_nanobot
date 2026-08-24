"""Каноническая санитизация текста сообщений/результатов инструментов.

PostgreSQL не принимает настоящий NUL-байт (0x00) в text-литералах
(``A string literal cannot contain NUL (0x00) characters.``), а psycopg2
трактует литеральные Unicode-escape ``\\u0000``..\\u0003`` как управляющие
символы (``UntranslatableCharacter``). Обе формы семантически — один и тот же
невалидный NUL, который может попасть в контент из бинарного вывода
инструментов (``exec``/``read_file``) или LLM-вывода.

``clean_text`` — единая точка вычистки. Она применяется:
  * на источнике — при добавлении сообщения в сессию
    (патч ``Session.add_message`` в ``lib/services/runtime_patcher.py``);
  * как страховка на границе БД — ``utils/db`` ``_sanitize_param``.
"""

from __future__ import annotations

from typing import Any

# Литеральные Unicode-escape, которые psycopg2 трактует как настоящие
# управляющие символы и не может отправить в PostgreSQL.
CLEAN_ESCAPES = ("\\u0000", "\\u0001", "\\u0002", "\\u0003")


def clean_text(value: Any) -> Any:
    """Рекурсивно вычистить NUL (0x00) и литеральные ``\\u0000``..\\u0003``.

    Проходит по строкам внутри строк/list/tuple/dict. Байтовые значения
    и не-контейнеры возвращаются без изменений.
    """
    if isinstance(value, str):
        if "\x00" in value or any(seq in value for seq in CLEAN_ESCAPES):
            cleaned = value.replace("\x00", "")
            for seq in CLEAN_ESCAPES:
                cleaned = cleaned.replace(seq, "")
            return cleaned
        return value
    if isinstance(value, (list, tuple)):
        return [clean_text(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_text(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return value
    return value
