"""Тесты ``lib.services.session_storage.install_async_save``.

Нативная замена патча ``RuntimePatcher.patch_async_session_saves``
(change ``enterprise-mcp-platform``, фаза 6, п. 6.3). Патч оборачивал
приватный ``agent.sessions.save``; обёртка ставится в
``SessionStorageService`` — единственной точке, где агент выбирает
менеджер сессий.

Инварианты:
  * внутри event loop сохранение уходит в executor и не блокирует loop
    (иначе async-транзакции канала не завершаются);
  * вне event loop поведение прежнее — синхронный вызов;
  * снимок сессии берётся на момент вызова, а не когда executor
    добежит;
  * ошибка сохранения логируется, но не ломает оборот.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from lib.services.session_storage import install_async_save


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


class _RecordingManager:
    """Менеджер сессий, который только запоминает вызовы."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, bool, str]] = []
        self.raise_on_save = False
        self.save_delay = 0.0

    def save(self, session: Any, *, fsync: bool = False) -> str:
        if self.save_delay:
            time.sleep(self.save_delay)
        self.calls.append((session, fsync, threading.current_thread().name))
        if self.raise_on_save:
            raise RuntimeError("boom")
        return "saved"


def _session(key: str = "s1", **overrides):
    from nanobot.session.manager import Session

    session = Session(key=key)
    for name, value in overrides.items():
        setattr(session, name, value)
    return session


def _shutdown(manager) -> None:
    executor = getattr(manager, "_async_save_executor", None)
    if isinstance(executor, ThreadPoolExecutor):
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Обёртка
# ---------------------------------------------------------------------------


class TestInstallAsyncSave:
    def test_none_manager_passthrough(self):
        assert install_async_save(None) is None

    def test_manager_without_save_passthrough(self):
        """Объект без ``save`` не должен падать при установке обёртки."""
        sentinel = object()
        assert install_async_save(sentinel) is sentinel

    def test_returns_same_manager(self):
        manager = _RecordingManager()
        assert install_async_save(manager) is manager

    def test_double_install_is_idempotent(self):
        """Повторная установка не оборачивает обёртку в обёртку.

        Без идемпотентности каждый вызов ``install_async_save`` плодил бы
        новый executor и новый слой отложенного сохранения.
        """
        manager = install_async_save(_RecordingManager())
        executor = manager._async_save_executor
        again = install_async_save(manager)
        assert again._async_save_executor is executor

    def test_marks_manager_as_wrapped(self):
        manager = install_async_save(_RecordingManager())
        assert manager._async_save_wrapped is True


class TestNonLoopCallRunsSynchronously:
    def test_runs_inline_and_returns_value(self):
        """Вне event loop поведение прежнее — синхронный вызов."""
        manager = install_async_save(_RecordingManager())
        session = _session()
        assert manager.save(session) == "saved"
        assert len(manager.calls) == 1
        assert manager.calls[0][0] is session  # без снимка
        _shutdown(manager)

    def test_fsync_forwarded_synchronously(self):
        manager = install_async_save(_RecordingManager())
        manager.save(_session(), fsync=True)
        assert manager.calls[0][1] is True
        _shutdown(manager)


class TestLoopCallDeferredToExecutor:
    def test_does_not_block_the_event_loop(self):
        """Медленный save не должен держать event loop.

        Главная причина переноса: sync-save блокировал loop и не давал
        завершиться async-транзакциям канала (poll/flush/lease).
        Проверяется на заведомо плохих данных — ``save_delay`` большой,
        колдун ждёт завершения executor'а и следит за порядком.
        """
        manager = install_async_save(_RecordingManager())
        manager.save_delay = 0.5
        order: list[str] = []
        event_loop_was_free = False

        async def main():
            nonlocal event_loop_was_free
            manager.save(_session())
            # loop свободен: успеваем выполнить следующий await до конца save.
            await asyncio.sleep(0.05)
            event_loop_was_free = True
            order.append("loop")

        t0 = time.perf_counter()
        asyncio.run(main())
        loop_elapsed = time.perf_counter() - t0
        order.append("executor-done")
        _shutdown(manager)

        assert event_loop_was_free, "event loop был заблокирован синхронным save"
        assert order == ["loop", "executor-done"], order
        # Синхронный save занял бы весь цикл (0.5с); вынесенный в executor
        # освобождает loop сразу — замеряем только сам asyncio.run(),
        # до join'а executor'а.
        assert loop_elapsed < 0.3, (
            f"save не был вынесен в executor ({loop_elapsed:.3f}s)"
        )

    def test_save_runs_on_separate_thread(self):
        manager = install_async_save(_RecordingManager())

        async def main():
            assert manager.save(_session()) is None, "в loop save не должен ждать"
            await asyncio.sleep(0.2)

        asyncio.run(main())
        _shutdown(manager)
        assert manager.calls, "executor не выполнил save"
        assert manager.calls[0][2] != threading.current_thread().name

    def test_snapshot_taken_at_call_time(self):
        """Снимок фиксируется на момент вызова, не когда executor добежит.

        Если бы executor получал живой объект, последующая мутация
        сессии попала бы на диск — а это ровно тот TOCTOU, который
        снимок и закрывает.
        """
        manager = install_async_save(_RecordingManager())
        manager.save_delay = 0.2
        session = _session()
        session.messages = [{"role": "user", "content": "before"}]

        async def main():
            manager.save(session)
            session.messages = [{"role": "user", "content": "AFTER-MUTATION"}]
            await asyncio.sleep(0.4)

        asyncio.run(main())
        _shutdown(manager)
        saved_session = manager.calls[0][0]
        assert saved_session is not session
        assert saved_session.messages == [{"role": "user", "content": "before"}]

    def test_snapshot_copies_message_list(self):
        """Список сообщений копируется, а не разделяется с оригиналом."""
        manager = install_async_save(_RecordingManager())
        messages = [{"role": "user", "content": "orig"}]
        session = _session()
        session.messages = messages

        async def main():
            manager.save(session)
            messages.append({"role": "user", "content": "appended-later"})
            await asyncio.sleep(0.2)

        asyncio.run(main())
        _shutdown(manager)
        assert manager.calls[0][0].messages == [{"role": "user", "content": "orig"}]

    def test_writes_are_ordered(self):
        """Единый executor с max_workers=1 сохраняет порядок записей."""
        manager = install_async_save(_RecordingManager())

        async def main():
            for i in range(5):
                session = _session(f"s{i}")
                session.messages = [{"role": "user", "content": f"m{i}"}]
                manager.save(session)
            await asyncio.sleep(0.3)

        asyncio.run(main())
        _shutdown(manager)
        assert [c[0].key for c in manager.calls] == [f"s{i}" for i in range(5)]


class TestFailuresAreNotPropagated:
    def test_save_error_in_loop_does_not_raise(self):
        """Ошибка в executor'е не должна доходить до агента."""
        manager = install_async_save(_RecordingManager())
        manager.raise_on_save = True

        async def main():
            assert manager.save(_session()) is None
            await asyncio.sleep(0.2)

        asyncio.run(main())  # не должно бросить
        _shutdown(manager)
        assert manager.calls, "executor не выполнил save"

    def test_save_error_outside_loop_still_raises(self):
        """Вне loop обёртка не перехватывает — поведение прежнее."""
        manager = install_async_save(_RecordingManager())
        manager.raise_on_save = True
        with pytest.raises(RuntimeError, match="boom"):
            manager.save(_session())
        _shutdown(manager)


# ---------------------------------------------------------------------------
# Дефект переноса: снимок теряет SessionPolicy
# ---------------------------------------------------------------------------


class TestSnapshotPreservesSessionPolicy:
    """Снимок обязан сохранять ``SessionPolicy``.

    ``SessionManager.save()`` начинается с ``if not session.policy.persist:
    return`` — политика решает, писать ли сессию вообще. Снимок в
    ``install_async_save`` собирает ``Session`` из шести полей и
    ``policy``/``provider_state`` НЕ переносит, поэтому снимок всегда
    получает дефолтный ``SessionPolicy`` (``persist=True``).

    Все тесты класса обязаны вызывать ``save`` **внутри** event loop:
    вне loop обёртка исполняет оригинал синхронно и снимок не строится
    вовсе — тест проходил бы, ничего не проверяя.
    """

    def _save_via_loop(self, manager, session) -> None:
        async def main():
            manager.save(session)
            await asyncio.sleep(0.2)

        asyncio.run(main())
        _shutdown(manager)

    def test_synchronous_path_does_not_snapshot(self):
        """Контроль самого теста: вне loop снимка нет.

        Если бы этот тест падал — значит обёртка начала строить снимок
        даже на синхронном пути, и весь класс ниже проверял бы не то.
        """
        manager = install_async_save(_RecordingManager())
        session = _session()
        manager.save(session)
        _shutdown(manager)
        assert manager.calls[0][0] is session

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "ПРОИЗВОДСТВЕННЫЙ ДЕФЕКТ (унаследованный, не внесён переносом): "
            "lib/services/session_storage.py, install_async_save._snapshot() "
            "собирает Session из полей key/messages/created_at/updated_at/"
            "metadata/last_consolidated и НЕ переносит session.policy и "
            "session.provider_state. SessionManager.save() начинается с "
            "'if not session.policy.persist: return', поэтому асинхронное "
            "сохранение сессии с persist=False всё равно пишет её на диск; "
            "policy.log_content=False (приватность) и disabled_tools тоже "
            "теряются. Воспроизведено: snapshot.policy == "
            "SessionPolicy(persist=True) при исходном persist=False. "
            "Код _snapshot дословно скопирован из удалённого патча "
            "patch_async_session_saves, то есть дефект перенесён, а не "
            "приобретён. Фикс: добавить policy=session.policy и "
            "provider_state=session.provider_state в _snapshot. "
            "Правка вне моего скоупа (production-код)."
        ),
    )
    def test_snapshot_carries_persist_false(self):
        from nanobot.session.manager import SessionPolicy

        manager = install_async_save(_RecordingManager())
        session = _session()
        session.policy = SessionPolicy(persist=False)

        self._save_via_loop(manager, session)
        saved = manager.calls[0][0]
        assert saved is not session, "снимок не построен — тест бессмысленен"
        assert saved.policy.persist is False, (
            "снимок потерял SessionPolicy: сессия с persist=False "
            "будет записана на диск, хотя владелец запретил персист"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Тот же дефект, что и в test_snapshot_carries_persist_false "
            "(см. reason там): _snapshot() не переносит session.policy и "
            "session.provider_state. Разделено на два теста, чтобы "
            "показать обе потерянные группы полей независимо."
        ),
    )
    def test_snapshot_preserves_all_session_fields(self):
        from nanobot.session.manager import SessionPolicy

        manager = install_async_save(_RecordingManager())
        session = _session()
        session.policy = SessionPolicy(persist=False, log_content=False)
        session.provider_state = "SENTINEL-PROVIDER-STATE"

        self._save_via_loop(manager, session)
        saved = manager.calls[0][0]
        assert saved.policy is session.policy
        assert saved.provider_state == "SENTINEL-PROVIDER-STATE"