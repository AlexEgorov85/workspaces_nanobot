"""Безопасное декодирование JSONB-значений, приходящих из psycopg2/asyncpg.

psycopg2 при ``register_json`` возвращает ``dict`` напрямую, но старые
записи в БД (до перехода на JSONB-колонку) могут храниться как
JSON-строка, а сторонние драйверы (Greenplum ODBC, asyncpg с другим
кодеком) — отдавать ``str``. Чтение из БД в любых каналах должно
проходить через одну функцию.
"""

from __future__ import annotations

import json
from typing import Any


def decode_jsonb(val: Any) -> dict:
    """Безопасно декодировать JSONB-значение из БД в ``dict``.

    Принимает:
      * ``None`` → ``{}``
      * ``str`` (JSON) → парсится через ``json.loads`` (пустая строка → ``{}``)
      * ``dict`` или Mapping → возвращается как есть / конвертируется в ``dict``
      * любое другое → ``{}`` (защита от битых типов)
    """
    if val is None:
        return {}
    if isinstance(val, str):
        return json.loads(val) if val else {}
    if isinstance(val, dict):
        return val
    return dict(val) if val else {}


def decode_json_list(val: Any) -> list:
    """Безопасно декодировать JSONB-список из БД в ``list``.

    Принимает:
      * ``None``/``""`` → ``[]``
      * ``str`` (JSON) → парсится через ``json.loads``
      * ``list`` → возвращается как есть
      * любое другое → ``[]``

    Эквивалент прежней ``streamlit_app._decode_media_list`` — единая точка
    для чтения JSONB-колонок-списков (например, ``media``).
    """
    if val is None:
        return []
    if isinstance(val, str):
        return json.loads(val) if val else []
    if isinstance(val, list):
        return val
    return []
