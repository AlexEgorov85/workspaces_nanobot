"""Контракт ``retry_on_exception`` — единственного определения в платформе.

Портирован вместе с ``libs/llm`` и покрыт отдельно, потому что им пользуется
не только вызов провайдера: ретрай — общий механизм, и тихая смена его формы
(например, «съесть» последнее исключение) ломает всех потребителей сразу.

Каждая проверка выполняется на заведомо плохих данных: retry, который ничего
не фильтрует, выглядит на исправном провайдере ровно так же, как правильный.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common import retry as retry_module  # noqa: E402
from libs.enterprise_common.retry import retry_on_exception  # noqa: E402


class _Boom(Exception):
    """Своё исключение: проверяем фильтр по списку, а не по классу Exception."""


class _Other(Exception):
    """Второй «свой» класс — чтобы отличить «не в списке» от «в списке»."""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Записать задержки вместо сна.

    Настоящий backoff в тесте — это минуты ожидания ради проверки, что
    кто-то позвонил в ``time.sleep``.
    """
    delays: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", delays.append)
    return delays


def _script(*failures: Exception) -> tuple[Any, list[int]]:
    """Вызываемый объект: сначала исключения, затем успешный результат."""
    calls: list[int] = []

    def run() -> str:
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return "ok"

    return run, calls


class TestSuccess:
    def test_returns_result_without_sleep(self, no_sleep: list[float]) -> None:
        run, calls = _script()
        assert retry_on_exception(run, exceptions=(_Boom,), max_retries=3) == "ok"
        assert len(calls) == 1
        assert no_sleep == []

    def test_unlisted_exception_is_not_retried(self, no_sleep: list[float]) -> None:
        """Фильтр по списку — иначе сбой валидации выглядел бы как сеть."""
        run, calls = _script(_Other("нет в списке"))
        with pytest.raises(_Other):
            retry_on_exception(run, exceptions=(_Boom,), max_retries=3)
        assert len(calls) == 1
        assert no_sleep == []


class TestBackoff:
    def test_delays_double_and_stop_at_ceiling(self, no_sleep: list[float]) -> None:
        run, calls = _script(*[_Boom("x") for _ in range(4)])
        assert retry_on_exception(
            run,
            exceptions=(_Boom,),
            max_retries=5,
            base_delay=1.0,
            max_delay=3.0,
        ) == "ok"
        assert len(calls) == 5
        assert no_sleep == [1.0, 2.0, 3.0, 3.0]

    def test_sleep_happens_between_attempts_only(self, no_sleep: list[float]) -> None:
        """После последней попытки сна нет: ждать уже нечего."""
        run, _ = _script(_Boom("x"), _Boom("y"))
        with pytest.raises(_Boom):
            retry_on_exception(run, exceptions=(_Boom,), max_retries=2)
        assert no_sleep == [1.0]

    def test_on_retry_number_overrides_delay(self, no_sleep: list[float]) -> None:
        run, _ = _script(_Boom("x"), _Boom("y"))
        seen: list[tuple[int, int, float]] = []

        def on_retry(attempt: int, max_retries: int, delay: float, exc: Exception) -> float:
            seen.append((attempt, max_retries, delay))
            return 0.5

        with pytest.raises(_Boom):
            retry_on_exception(
                run,
                exceptions=(_Boom,),
                max_retries=2,
                base_delay=10.0,
                label="llm",
                on_retry=on_retry,
            )
        assert seen == [(1, 2, 10.0)]
        assert no_sleep == [0.5]

    def test_on_retry_none_keeps_standard_backoff(self, no_sleep: list[float]) -> None:
        run, _ = _script(_Boom("x"), _Boom("y"))
        with pytest.raises(_Boom):
            retry_on_exception(
                run,
                exceptions=(_Boom,),
                max_retries=2,
                base_delay=1.0,
                on_retry=lambda *_: None,
            )
        assert no_sleep == [1.0]

    def test_on_retry_can_re_raise_to_abort(self, no_sleep: list[float]) -> None:
        """Хук, рейзящий исключение, немедленно обрывает цикл.

        На этом стоит политика клиента LLM: ретраится 429, а 500 пробрасывается.
        """
        run, calls = _script(_Other("не ретраить"))

        def on_retry(attempt: int, max_retries: int, delay: float, exc: Exception) -> None:
            raise _Other("прервано хуком")

        with pytest.raises(_Other, match="прервано хуком"):
            retry_on_exception(
                run,
                exceptions=(_Boom, _Other),
                max_retries=5,
                on_retry=on_retry,
            )
        assert len(calls) == 1
        assert no_sleep == []


class TestExhaustion:
    def test_last_exception_propagates(self, no_sleep: list[float]) -> None:
        run, calls = _script(_Boom("последняя"), _Boom("вторая"), _Boom("первая"))
        with pytest.raises(_Boom, match="первая"):
            retry_on_exception(run, exceptions=(_Boom,), max_retries=3)
        assert len(calls) == 3
        assert no_sleep == [1.0, 2.0]

    def test_zero_retries_still_calls_once(self, no_sleep: list[float]) -> None:
        """``max_retries=0`` — дефолт ``call_llm_json``: один вызов, без ретрая."""
        run, calls = _script(_Boom("сразу"))
        with pytest.raises(_Boom):
            retry_on_exception(run, exceptions=(_Boom,), max_retries=0)
        assert len(calls) == 1
        assert no_sleep == []


class TestGuardHasTeeth:
    """Проверка самих проверок: «сторож, который ни разу не срабатывал,
    неотличим от стража, который ничего не проверяет»."""

    def test_assertion_fails_on_swallowing_retry(self, no_sleep: list[float]) -> None:
        """Мусорная реализация «повторяет всё и не пробрасывает» не проходит
        тот же контракт, что и настоящая."""
        def broken(fn: Any) -> Any:  # noqa: ANN401 - мусорная фикстура
            for _ in range(3):
                try:
                    return fn()
                except Exception:  # noqa: BLE001 - так делает неправильный retry
                    continue
            return None

        run, _ = _script(_Other("ошибка"))
        with pytest.raises(_Other):
            broken(retry_on_exception(run, exceptions=(_Boom,), max_retries=3))

    def test_logging_does_not_swallow_result(self, caplog: pytest.LogCaptureFixture) -> None:
        run, calls = _script(_Boom("шум в лог"), _Boom("и снова"))
        with caplog.at_level(logging.WARNING, logger=retry_module.__name__):
            with pytest.raises(_Boom):
                retry_on_exception(run, exceptions=(_Boom,), max_retries=2, label="llm")
        assert "llm retry 1/2" in caplog.text
        assert len(calls) == 2
