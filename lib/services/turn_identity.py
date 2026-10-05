"""Личность оборота: чей вход, чей вопрос. Хранилище принадлежит рантайму.

Зачем отдельный объект. Личность оборота — это ``{user_id, request_id, at_seq}``,
и её знают две независимые подсистемы: журнал (вход регистрируется при приёме
сообщения) и подписчик runtime-событий (ему нужно подписать ``agent.completed``,
когда оборот уже кончился). Раньше хранилищем служил словарь внутри
``DbLoggingService``, и получалось, что журнал — и писатель, и чужое
хранилище: подписчику приходилось брать чужое поле.

Почему не contextvar. ``RequestContext`` живёт в contextvar задачи оборота.
Подписчик событий работает в СВОЕЙ задаче, и contextvar туда не заходит: в
боевом логе это стоило подписи каждого ``agent.completed`` — следом идущий
``agent.delivered`` подписывался (он идёт через журнал), а ``completed`` уходил
неподписанным. Событие — тоже не носитель: его контекст собирает nanobot, и
``sender_id`` туда не попадает.

Почему журнал при этом не выводит личность сам. Он этого не делает намеренно:
для события без вопроса снимок принимается, только если в снимке САМОМ вопроса
нет, иначе событие утечёт в чужой scope (см.
``DbLoggingService._resolve_event_user_id_without_request``). Подписать событие
оборота обязан тот, кто знает, что это за оборот, — подписчик, и личность он
берёт здесь, у её владельца.

Одноразовость. ``take()`` забирает снимок ровно один раз и принадлежит финальной
доставке ответа: забрал бы кто-то ещё — ``agent.delivered`` остался бы без
подписи. За это отвечает страж ``tests/test_final_delivery_is_signed.py``.
"""

from __future__ import annotations

import threading
from typing import Any


class TurnIdentityStore:
    """По ``session_key`` — личность ВХОДА, начавшего текущий оборот.

    Потокобезопасен: сессии обрабатываются конкурентно, а писателей и читателей
    минимум два. Возвращаются копии: читатель не должен уметь изменить снимок.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, dict[str, Any]] = {}

    def record(self, session_key: str, snapshot: dict[str, Any]) -> None:
        """Положить личность входа сессии. Новый вход заменяет предыдущий.

        Пара «индекс вопроса + снимок» пишется под двумя разными замками, и
        читатель может увидеть новый снимок при старом индексе. Это допустимо:
        снимок несёт ``at_seq`` и сверяется с моментом события, а индекс
        сверяется по ``request_id`` — обе гонки закрыты на стороне читателя.
        """
        if not session_key:
            return
        with self._lock:
            self._by_session[str(session_key)] = dict(snapshot)

    def current(self, session_key: str | None) -> dict[str, Any] | None:
        """Прочитать личность ВХОДА, НЕ изымая. ``None`` — входа не было."""
        if not session_key:
            return None
        with self._lock:
            found = self._by_session.get(str(session_key))
            return dict(found) if isinstance(found, dict) else None

    def take(self, session_key: str | None) -> dict[str, Any] | None:
        """Забрать личность ВХОДА — ровно один раз (финальная доставка ответа)."""
        if not session_key:
            return None
        with self._lock:
            return self._by_session.pop(str(session_key), None)

    def clear(self) -> None:
        with self._lock:
            self._by_session.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_session)
