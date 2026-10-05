"""Страж на «второе хранилище» — регрессия, найденная на живом прогоне.

Симптом был виден только в бою: подписчик писал «стор передан, записей в нём
0» через 8 мс после регистрации входа. Причина — ``DbLoggingService`` выбирал
хранилище выражением ``turn_identities or TurnIdentityStore()``, а у
``TurnIdentityStore`` был ``__len__``: пустое хранилище ложно, ``or`` срабатывал,
и журнал завёл себе второе. Писал в одно, читатель читал из другого.

Тесты ниже бьют именно по этому, а не «проверяют, что поле передаётся».
"""
from __future__ import annotations

from config import runtime_table
from lib.services.db_logging_service import DbLoggingService
from lib.services.turn_identity import TurnIdentityStore

SESSION = "postgres:chat_alice_1"


def _service(store: TurnIdentityStore | None) -> DbLoggingService:
    return DbLoggingService(
        dsn="",
        table_name=runtime_table("gateway_logs"),
        question_runs_table=runtime_table("question_runs"),
        turn_identities=store,
    )


class TestSingleStoreOwner:
    """Владелец личности оборота один, даже когда он ещё пуст."""

    def test_empty_store_is_not_mistaken_for_absence(self) -> None:
        """Пустое хранилище — это хранилище, а не «хранилище не передали».

        Проверка ловит ровно ту ошибку: пока у ``TurnIdentityStore`` есть
        ``__len__``, пустой объект ложен и ``or`` создаёт второй.
        """
        store = TurnIdentityStore()
        assert bool(store) is True, (
            "хранилище личности не должно быть ложным, пока пусто: "
            "проверка `store or TurnIdentityStore()` создаст второе"
        )

    def test_journal_does_not_mint_a_second_store(self) -> None:
        """Журнал берёт ПЕРЕДАННОЕ хранилище, а не своё.

        Именно этот сценарий и разошёлся в бою: хранилище контекста было
        пустым, журнал записал снимок входа в своё второе, а подписчик
        прочитал чужое — всегда пустое.
        """
        store = TurnIdentityStore()
        service = _service(store)

        assert service.turn_identities is store, (
            "журнал завёл себе второе хранилище личности — писатель и читатель "
            "разошлись, и читатель не увидит ничего"
        )

        # Сквозная проверка: то, что пишет журнал, обязан видеть владелец.
        service.register_request(SESSION, "req-1", user_id="alice", chat_id="c1")
        record = store.current(SESSION)
        assert record is not None, "владелец не увидел запись, которую сделал журнал"
        assert record.user_id == "alice"

    def test_subscriber_and_journal_share_one_store(self) -> None:
        """Подписчик и журнал ходят в ОДИН экземпляр — как в бою.

        Собираются настоящие классы, как в ``ApplicationContext.start``:
        журнал получает стор, подписчик — тот же.
        """
        import threading

        from lib.services.runtime_events_subscriber import RuntimeEventsSubscriber

        store = TurnIdentityStore()
        service = _service(store)
        subscriber = RuntimeEventsSubscriber.__new__(RuntimeEventsSubscriber)
        subscriber._turn_identities = store
        subscriber._db_logging_service = service
        subscriber._identity_lock = threading.Lock()
        subscriber._turn_identities_seen = {}

        service.register_request(SESSION, "req-1", user_id="alice", chat_id="c1")
        subscriber._capture_identity(SESSION)

        assert subscriber._turn_identities_seen, (
            "подписчик не увидел личность оборота, который журнал только что "
            "зарегистрировал"
        )
        captured = subscriber._turn_identities_seen[SESSION]
        assert captured.user_id == "alice"
        assert captured.request_id == "req-1"

    def test_store_counts_explicitly(self) -> None:
        """Число записей берётся явно: ``len()`` у хранилища быть не должно.

        Наличие ``len`` и есть ловушка, поэтому проверяется и сам метод, и то,
        что он считает то же, что и словарь внутри.
        """
        store = TurnIdentityStore()
        assert store.count() == 0
        store.start_turn(SESSION, user_id="alice", request_id="r", at_seq=1)
        assert store.count() == 1
        assert not hasattr(store, "__len__"), (
            "__len__ у хранилища личности делает пустой объект ложным — "
            "любая проверка по истинности создаст второе хранилище"
        )
