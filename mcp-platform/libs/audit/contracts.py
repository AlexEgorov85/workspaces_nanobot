"""Зависимости конвейера аудита от внешнего мира — только как callable.

Правило одно: библиотека аудита **не знает, как читаются данные и как
зовётся модель**. Всё, что умеет ходить наружу, приходит колбэком от
владельца (capability ``audit`` получает их у capability ``data`` и у
``libs/llm``). Причины ровно те же, что у ``libs/vectors``:

* HTTP-клиент принадлежит ``libs/llm``, а ``libs/audit`` не должен ни
  знать про него, ни поднимать (архитектурный страж это проверяет);
* чтение снимка принадлежит ``libs/enterprise_data``, а ещё и capability
  ``data``: обход означал бы второй путь к данным мимо владельца снимка.

Имена параметров в сигнатурах **вызывающей** стороны (у ``DataService`` это
``sql``/``params``) намеренно не воспроизводятся: описывается позиционный
интерфейс, а не чужое имя аргумента. Вызывающий передаёт готовый bound-метод
как есть.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = [
    "SnapshotQuery",
    "SnapshotSchema",
    "SnapshotExplain",
    "ChatCallable",
]

#: Чтение снимка. Соответствует ``DataService.snapshot_query(text, params)``.
#: Возвращает словарь с ``status``/``row_count``/``columns``/``rows``
#: (либо ``status="error"`` и ``error``).
SnapshotQuery = Callable[[str, "list[Any] | None"], "dict[str, Any]"]

#: Описание схемы снимка. Соответствует ``DataService.snapshot_schema()``
#: с уже подставленными аргументами; возвращает словарь в форме,
#: которую понимает ``libs.enterprise_data.sql_safety.format_schema``.
SnapshotSchema = Callable[[], "dict[str, Any]"]

#: ``EXPLAIN`` без выполнения. Соответствует результату
#: ``libs.enterprise_data.snapshot.query.explain_query``:
#: ``{"valid": True, "plan": [...]}`` либо ``{"valid": False, "error": "..."}``.
SnapshotExplain = Callable[[str], "dict[str, Any]"]

#: Вызов модели. Соответствует ``libs.llm.client.call_llm(messages, ...)``:
#: на вход — список сообщений, на выход — текст ответа.
#:
#: Вызывающая сторона обязана подставить личность и конфиг сама (например
#: ``lambda messages: call_llm(messages, cfg=cfg)``). Аргумента ``context``
#: здесь нет намеренно: в агенте он позволял подклеить текст вызывающей
#: стороны в начало сообщений генератору (пункт 4.9).
ChatCallable = Callable[["list[dict[str, Any]]"], str]
