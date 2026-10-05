"""Личность оборота: чей вход, чей вопрос. Единственный владелец — этот модуль.

Зачем отдельный объект. Личность оборота — это ``{user_id, request_id, at_seq}``,
и её знают независимые подсистемы: журнал (вход регистрируется при приёме
сообщения) и подписчик runtime-событий (ему нужно подписать ``agent.completed``,
когда оборот уже кончился). Хранилищем служил словарь внутри
``DbLoggingService``, и получалось, что журнал — и писатель, и чужое
хранилище: подписчику приходилось брать чужое поле.

Почему не contextvar. ``RequestContext`` живёт в contextvar задачи оборота.
Подписчик событий работает в СВОЕЙ задаче, и contextvar туда не заходит: в
боевом логе это стоило подписи каждого ``agent.completed`` — следом идущий
``agent.delivered`` подписывался (он идёт через журнал), а ``completed`` уходил
неподписанным. Событие — тоже не носитель: его контекст собирает nanobot, и
``sender_id`` туда не попадает.

Почему одна запись, а не два словаря. Раньше личность лежала в ДВУХ носителях:
индекс «текущий вопрос» внутри журнала (``_request_index``) и снимок входа
здесь. Оба писались в одной строке ``register_request``, но под РАЗНЫМИ
замками, поэтому пара «индекс + снимок» не была атомарной: читатель мог увидеть
новый снимок при старом индексе. Сейчас это одна запись под одним замком, и
гонки нет — обе части пишутся одним присваиванием.

Две части записи имеют РАЗНЫЕ сроки жизни, и это не двойное хранение, а
разные вопросы:

* ``{user_id, request_id, at_seq}`` — снимок ВХОДА. Живёт дольше вопроса:
  финальный ответ оборота приходит уже после ``release_question``, и подписать
  его больше нечем.
* ``question_live`` — привязка вопроса. Снимается ``release_question`` в конце
  оборота; после этого ``get_request_id`` отвечает ``None``, как отвечала
  снятая запись индекса.

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
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class TurnIdentity:
    """Личность ВХОДА сессии и её привязка к вопросу — одна запись.

    Неизменяемая намеренно: читатель возвращает копию, и подписать событие
    чужой личностью нельзя не только по недосмотру, но и по ошибке вызывающего
    (объект нельзя доиграть под существующее событие).
    """

    #: Отправитель входа. Известен только на входе и больше нигде в обороте.
    user_id: str | None = None
    #: Вопрос, с которым пришёл вход. ``None`` — вопроса не было (фоновый вызов,
    #: правка в ``cli``, служебная проба). Это НЕ сломанный вопрос.
    request_id: str | None = None
    #: Момент появления записи по шкале, которую задаёт вызывающий
    #: (``next_event_seq`` у журнала). Читатель сверяет его с моментом события,
    #: чтобы отложенное событие не унаследовало личность СЛЕДУЮЩЕГО входа.
    at_seq: int = 0
    #: Жива ли ещё привязка вопроса. Снимается ``release_question``; снимок
    #: входа при этом остаётся, потому что финальный ответ приходит позже.
    question_live: bool = False


@dataclass(frozen=True)
class TurnContext:
    """Что нанобот знает об обороте: сессия и отправитель.

    Отдельный объект, а не разрозненные строки, потому что читать это должен
    ровно ОДИН раз — ``read_turn_context`` ниже. ``sender_id`` из того же
    contextvar читался в шести местах, и смена способа его получения
    означала бы правку шести копий, расходящихся молча.
    """

    session_key: str | None = None
    user_id: str | None = None


def read_turn_context() -> TurnContext | None:
    """Личность оборота из contextvar нанобота, или ``None``.

    Единственное место в проекте, где читается ЛИЧНОСТЬ оборота: и хук
    ``mcp_enterprise_*``, и клиент платформы, и подписчик событий брали
    ``sender_id`` каждый по-своему, и расхождение между ними было бы молчаливым —
    подпись в журнале и файл сессии описали бы разные вызовы.

    Не «единственное место, где читается ``RequestContext``»: его читают и ради
    других полей (``context_compaction`` — ради ``message_id``). Узкое
    утверждение сделано потому, что широкое было бы ложью, а ложь в докстринге
    этого модуля обесценивает всё остальное, что здесь написано.

    Импорт ленивый и внутри ``try``: нанобот — библиотека, и её отсутствие
    обязано давать «нет личности», а не падение на импорте модуля.

    ``None`` вместо объекта с пустыми полями — чтобы вызывающий не мог
    потерять различие между «контекста нет» и «контекст есть, но он пуст»:
    во втором случае подпись всё равно невозможна, а различать их незачем.
    """
    # Два поля читаются НЕЗАВИСИМО, и это не избыточность. Отправитель нужен
    # всем, а сессия — не всем, и оба приходят из модуля нанобота. Единый
    # ``from ... import (a, b)`` падал бы целиком, стоит наноботу не отдать
    # одну из двух функций, и отправитель, который к этому моменту уже
    # прочитан, обнулился бы вместе с ней. Проверено: подмена модуля
    # заглушкой без помощника сессии возвращала ``None`` вместо ``sender_id``
    # (``tests/test_queue_anchor_identity.py``).
    try:
        from nanobot.agent.tools.context import current_request_context
    except Exception:  # noqa: BLE001 - нанобот может не отдавать модуль
        return None
    try:
        turn = current_request_context()
    except Exception:  # noqa: BLE001 - тот же контракт, что и у клиента
        return None
    if turn is None:
        return None

    sender_id = getattr(turn, "sender_id", None)
    user_id = sender_id if isinstance(sender_id, str) and sender_id else None

    try:
        from nanobot.agent.tools.context import current_request_session_key

        session_key = current_request_session_key()
    except Exception:  # noqa: BLE001 - сессия приходит вторым каналом
        session_key = getattr(turn, "session_key", None)

    return TurnContext(
        session_key=(
            str(session_key)
            if isinstance(session_key, str) and session_key.strip()
            else None
        ),
        user_id=user_id,
    )


#: Плоские ключи личности вызова. Имена обязаны совпадать с
#: ``LEGACY_IDENTITY_KEYS`` в ``mcp-platform/libs/enterprise_common/
#: execution/pipeline.py:71`` — сервер читает ровно эти три и вырезает их из
#: аргументов. Расхождение имён не падает, а молча ломает изоляцию вызова.
#:
#: Объявлено РЯДОМ со сборкой и кормит её: набор ключей и словарь, который из
#: него делается, не могут разойтись. Прежде объявление жило в хуке, а сборка
#: была литералом в двух модулях, и переименование ключа чинило одно место, а
#: второе молча уходило в ``identity_missing`` на живом вызове.
IDENTITY_KEYS = ("session_id", "user_id", "request_id")


def call_identity(
    session_key: str, user_id: str, request_id: str
) -> dict[str, str]:
    """Три ключа личности вызова — единственная сборка этого словаря."""
    return dict(zip(IDENTITY_KEYS, (session_key, user_id, request_id)))


class TurnIdentityStore:
    """По ``session_key`` — личность ВХОДА и привязка вопроса. ОДИН носитель.

    Потокобезопасен: сессии обрабатываются конкурентно, а писателей и читателей
    минимум три — журнал, подписчик событий и хук подписи вызовов.

    **Здесь намеренно нет ``__len__``.** Объект с ``__len__`` ложен, пока он
    пуст, и любая проверка ``store or TurnIdentityStore()`` или ``if store:``
    в будущем не отличит «хранилища не передали» от «хранинилище пусто» — и
    создаст второе хранилище именно тогда, когда личность ещё не накопилась.
    Так и вышло: ``DbLoggingService`` выбирал хранилище выражением
    ``turn_identities or TurnIdentityStore()``, при пустом хранилище контекста
    ``or`` срабатывал, журнал завёл второе — и подписчик читал из чужого
    всегда пустого. Число записей доступно только явно, через ``count()``.
    """

    def count(self) -> int:
        """Сколько сессий сейчас в хранилище. Явно, а не через ``len``."""
        with self._lock:
            return len(self._by_session)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, TurnIdentity] = {}

    # -- запись -----------------------------------------------------------

    def start_turn(
        self,
        session_key: str,
        *,
        user_id: str | None,
        request_id: str,
        at_seq: int,
    ) -> None:
        """Новый вход С вопросом: запись заменяется целиком.

        Новый вход заменяет предыдущий, и привязка вопроса становится живой:
        ``get_request_id`` отвечает ``request_id`` до ``release_question``.

        Запись целиком, а не по полям, — поэтому пара «снимок + привязка»
        появляется одним присваиванием под одним замком. Раньше эти две части
        писались отдельно, и читатель мог увидеть новый снимок при старом
        индексе вопроса.
        """
        if not session_key:
            return
        with self._lock:
            self._by_session[str(session_key)] = TurnIdentity(
                user_id=user_id,
                request_id=request_id,
                at_seq=at_seq,
                question_live=True,
            )

    def note_caller(
        self, session_key: str, *, user_id: str | None, at_seq: int
    ) -> None:
        """Новый вход БЕЗ вопроса: обновляется только снимок.

        Привязка вопроса намеренно НЕ трогается: у входа без вопроса нет
        активного оборота, и сбрасывать чужую привязку значило бы стереть
        вопрос, который ещё идёт. Снимок при этом становится «без вопроса» —
        именно это проверяет журнал, прежде чем подписать событие личностью
        такого входа (см. ``_resolve_event_user_id_without_request``).
        """
        if not session_key:
            return
        with self._lock:
            key = str(session_key)
            previous = self._by_session.get(key)
            self._by_session[key] = replace(
                previous if previous is not None else TurnIdentity(),
                user_id=user_id,
                request_id=None,
                at_seq=at_seq,
            )

    def release_question(self, session_key: str) -> None:
        """Снять привязку вопроса в конце оборота.

        Снимок входа остаётся: финальный ответ оборота приходит после этого
        вызова, и без снимка он ушёл бы неподписанным. Прежний ``clear_request``
        вёл себя так же — снимал только индекс, — и тест на группируемость
        ``agent.delivered`` после ``clear_request`` (``tests/test_turn_identity.py``)
        держит именно это поведение.
        """
        if not session_key:
            return
        with self._lock:
            key = str(session_key)
            previous = self._by_session.get(key)
            if previous is None:
                return
            self._by_session[key] = replace(previous, question_live=False)

    # -- чтение -----------------------------------------------------------

    def get_request_id(self, session_key: str | None) -> str | None:
        """``request_id`` ЖИВОГО вопроса сессии или ``None``.

        ``None`` после ``release_question`` — это и есть прежнее поведение
        снятой записи индекса, на которую опираются хук подписи вызовов и
        журнал.
        """
        record = self._live_question(session_key)
        return record.request_id if record is not None else None

    def question_of(self, session_key: str | None) -> TurnIdentity | None:
        """Запись сессии, только если привязка вопроса ещё жива."""
        return self._live_question(session_key)

    def identity_of_request(
        self, request_id: str
    ) -> tuple[str, str | None] | None:
        """Найти ``(session_key, user_id)`` ЖИВОГО вопроса по его ``request_id``.

        Обратный lookup для ``finish_request``: к моменту его вызова session_key
        у вызывающего уже нет, а личность вопроса ещё нужна. Ищется только по
        живым привязкам и только точным совпадением — чужая не подставляется,
        отсутствующая не выдумывается, тогда вызывающий получит неподписанную
        запись и посчитает её пропущенной, а не записанной.
        """
        if not request_id:
            return None
        with self._lock:
            for key, record in self._by_session.items():
                if not record.question_live:
                    continue
                if record.request_id != request_id:
                    continue
                return key, record.user_id
        return None

    def current(self, session_key: str | None) -> TurnIdentity | None:
        """Прочитать личность ВХОДА, НЕ изымая. ``None`` — входа не было.

        Снимок не зависит от ``question_live``: подписчик событий читает его
        уже после конца оборота, когда привязка вопроса снята, а личность
        входа всё ещё нужна.
        """
        if not session_key:
            return None
        with self._lock:
            return self._by_session.get(str(session_key))

    def take(self, session_key: str | None) -> TurnIdentity | None:
        """Забрать личность ВХОДА — ровно один раз (финальная доставка ответа)."""
        if not session_key:
            return None
        with self._lock:
            return self._by_session.pop(str(session_key), None)

    # -- служебное --------------------------------------------------------

    def _live_question(self, session_key: str | None) -> TurnIdentity | None:
        if not session_key:
            return None
        with self._lock:
            record = self._by_session.get(str(session_key))
        if record is None or not record.question_live:
            return None
        return record

    def clear(self) -> None:
        with self._lock:
            self._by_session.clear()



def request_id_from(source: Any, session_key: str | None) -> str | None:
    """Спросить ``request_id`` оборота у носителя личности.

    Единственное место, где спрашивают. Отказ носителя — не отказ вызова: пустое
    значение выражает «связи с ``agent_question_runs`` нет», и вызов уходит
    именно так; выдумывать идентификатор оборота нельзя, потому что под ним в
    журнале не найдётся ни одной строки.

    ``source`` — не типизированный протокол, а любой носитель с
    ``get_request_id``: так одна и та же функция обслуживает и хранилище, и
    подставной двойной в тестах, не заставляя тесты знать про два разных
    способа спросить одно и то же.
    """
    if not session_key:
        return None
    reader = getattr(source, "get_request_id", None)
    if reader is None:
        return None
    try:
        value = reader(session_key)
    except Exception:  # noqa: BLE001 - индекс, а не граница изоляции
        return None
    return str(value) if value else None
