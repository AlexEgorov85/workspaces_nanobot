"""Единственный писатель событий платформы.

Почему класс, а не функция: событие уходит в **две** формы — в журнал
(`agent_gateway_logs`) и, по флагу, в файл сессии. Обе записи обязаны нести
один и тот же `id`, иначе по журналу и по каталогу сессии получится два разных
события, а это ровно та расхождённость, ради устранения которой модель событий
и вводилась.

Буфера у писателя своего нет. Переполнение и дроп — забота приёмника
(`EventBuffer` внутри capability `data`): второй буфер означал бы, что
переполнение считается в двух местах, а агент видит одно число.

Петля журналирования не замыкается: событие о сбое записи события писатель не
создаёт, иначе отказ записи породил бы новые события и наполнил бы буфер, из
которого не пишется.

Порог у писателя **не свой**: он приходит от оператора (агент объявляет его в
``config.json → gateway.agent.logging.db.min_level`` и передаёт флагом запуска)
и доезжает с тем же значением, что и до писателя журнала capability ``data``.
Два фильтра на одном пути означали бы, что внутренние события платформы
отбрасываются по одному правилу, а события агента — по другому.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..session.workspace import SessionWorkspace
from .models import DEFAULT_MIN_LEVEL, AgentEvent, is_at_least, normalize_level
from .types import is_known, is_probe_event_type

log = logging.getLogger(__name__)

#: Приёмник строки журнала. В рантайме это ``DataService.accept`` capability
#: ``data``; подменяется в тестах.
EventSink = Callable[[dict[str, Any]], str | None]

#: Метки, которые :meth:`EventWriter.emit` возвращает вызывающему.
ACCEPTED = "accepted"
DROPPED = "dropped"
REJECTED = "rejected"
NO_SINK = "no_sink"
#: Событие не дошло до журнала **по правилу**, а не из-за отказа приёмника:
#: пробное имя либо уровень ниже порога. Отдельная метка, потому что у
#: ``DROPPED`` и ``REJECTED`` своя причина, и по одной строке «dropped» не
#: отличить переполнение буфера от тихого вычищения шума.
SUPPRESSED = "suppressed"


class EventWriter:
    """Отправка события в журнал и, по флагу, в файл сессии."""

    def __init__(
        self,
        sink: EventSink | None = None,
        *,
        workspace: SessionWorkspace | None = None,
        persist_session_events: bool = False,
        min_level: str | None = DEFAULT_MIN_LEVEL,
    ) -> None:
        self._sink = sink
        self._workspace = workspace
        self._persist = bool(persist_session_events and workspace is not None)
        # Порог нормализуется один раз, на создании: сравнение на каждом
        # событии с сырой строкой означало бы, что опечатка в пороге держится
        # до первого события и падает уже в рантайме.
        #
        # ``None`` — «порог не задан», то есть пишем всё, и это **тот же**
        # смысл, что у писателя журнала capability ``data``
        # (``_journal_min_level``). Значение по умолчанию — офлайн-дефолт
        # (сборка без оператора, тест), а не рантайм-значение: в рантайме порог
        # приходит от агента флагом ``--log-min-level``, и второй фильтр,
        # живший здесь по умолчанию, резал внутренние события платформы по
        # ``INFO`` независимо от того, что выбрал оператор. Проверяет это
        # ``tests/test_journal_writer_threshold_wiring.py``.
        self._min_level = normalize_level(min_level) if min_level is not None else None
        self._accepted = 0
        self._dropped = 0
        self._rejected = 0
        self._suppressed_probe = 0
        self._suppressed_noise = 0
        self._mirrored = 0
        self._mirror_failed = 0
        self._warned_no_sink = False
        self._warned_probe = False
        self._warned_noise = False

    @property
    def persists_session_events(self) -> bool:
        return self._persist

    @property
    def min_level(self) -> str | None:
        return self._min_level

    def emit(self, event: AgentEvent) -> str:
        """Отправить событие. Исключений не бросает никогда.

        Поверь и отказы: потеря события не должна превращаться в отказ операции,
        поэтому ни неизвестный тип, ни отсутствие приёмника, ни ошибка записи в
        файл сессии не поднимаются наружу — они считаются и видны в
        :meth:`stats`.
        """
        # Пробное имя проверяется **до** уровня: это нарушение контракта, а не
        # решение о настройке, и оно не должно выглядеть так, будто событие
        # отброшено порогом. В файл сессии оно тоже не попадает — иначе
        # вычистка таблицы оставила бы тот же мусор в каталогах сессий.
        if is_probe_event_type(event.event_type):
            self._suppressed_probe += 1
            self._warn_probe_once(event.event_type)
            return SUPPRESSED
        if self._min_level is not None and not is_at_least(event.level, self._min_level):
            self._suppressed_noise += 1
            self._warn_noise_once(event.level)
            return SUPPRESSED
        if not is_known(event.event_type):
            self._rejected += 1
            log.warning("событие отклонено: неизвестный тип %r", event.event_type)
            return REJECTED
        if self._sink is None:
            self._rejected += 1
            self._warn_no_sink_once()
            # Файл сессии — второе представление того же события, и журнала он
            # не требует. Без этой строки сервер, поднятый без capability
            # ``data`` (режим ``--capabilities llm``, в котором работают
            # подпроцессы скиллов), не писал бы о вызовах вообще ничего:
            # журнала нет, значит и зеркала нет, а ``persist_session_events``
            # выглядел бы включённым и не делал бы ничего.
            self._mirror(event)
            return NO_SINK

        row = event.to_row()
        try:
            marker = self._sink(row)
        except Exception as exc:  # noqa: BLE001 - запись события не ломает ход
            self._dropped += 1
            log.warning("событие %s не записано: %s", event.event_type, exc)
            return DROPPED
        if marker is not None:
            self._dropped += 1
            return DROPPED
        self._accepted += 1
        self._mirror(event)
        return ACCEPTED

    def _warn_probe_once(self, event_type: str) -> None:
        """Один раз сказать, что пробное имя в журнал не пишется.

        Один раз, а не на каждое событие: иначе вычистка шума заменила бы
        мусор в таблице мусором в operational-логе, и её объём не падал бы.
        Молчать тоже нельзя — иначе выключенная вычистка выглядит как «таких
        событий просто нет».
        """
        if self._warned_probe:
            return
        self._warned_probe = True
        log.warning(
            "событие %r не записано: пробное имя не пишется в продовую таблицу "
            "журнала (счётчик suppressed_probe в stats())",
            event_type,
        )

    def _warn_noise_once(self, level: str) -> None:
        """Один раз сказать, что уровень ниже порога.

        Событие не отбрасывается молча: порог виден в ``stats()`` как
        ``min_level``, число отброшенных — как ``suppressed_noise``, и оба
        значения попадают в ``execution.stats()``.
        """
        if self._warned_noise:
            return
        self._warned_noise = True
        log.warning(
            "события уровня %s не пишутся: порог журнала %s (счётчик suppressed_noise в stats())",
            level,
            self._min_level,
        )

    def _warn_no_sink_once(self) -> None:
        """Один раз сказать, что журнала нет.

        Не на каждое событие: иначе сервер без capability ``data``
        заполняет журнал предупреждениями вместо событий. Молчать тоже нельзя
        — иначе полная и внешне здоровая запись выглядит как работающая.
        """
        if self._warned_no_sink:
            return
        self._warned_no_sink = True
        log.warning(
            "у писателя событий нет журнала: capability `data` не поднята, "
            "события не попадут в agent_gateway_logs%s",
            "; файловая копия сессии включена" if self._persist else "",
        )

    def _mirror(self, event: AgentEvent) -> None:
        """Второе представление события — файл сессии, с тем же ``id``."""
        if not self._persist or self._workspace is None:
            return
        assert self._workspace is not None
        if not event.session_id:
            # Событие без `session_id` некуда положить: каталог сессии выводится
            # из него. Тихое сохранение в общий каталог смешало бы сессии.
            self._mirror_failed += 1
            return
        record = {
            "id": event.id,
            "event_type": event.event_type,
            "level": event.level,
            "name": event.name,
            "summary": event.summary,
            "session_id": event.session_id,
            "user_id": event.user_id,
            "request_id": event.request_id,
            "channel": event.channel,
            "actor": event.actor,
            "payload": event.payload,
            "metadata": event.metadata,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }
        try:
            self._workspace.write_json(
                event.session_id,
                f"{event.id}.json",
                record,
                subdir="events",
            )
        except Exception as exc:  # noqa: BLE001 - второе представление не важнее журнала
            self._mirror_failed += 1
            log.warning("копия события %s не записана: %s", event.id, exc)
            return
        self._mirrored += 1

    def stats(self) -> dict[str, int | bool | str | None]:
        return {
            "accepted": self._accepted,
            "dropped": self._dropped,
            "rejected": self._rejected,
            # Отбрасывание по правилу — отдельные счётчики, а не общий
            # ``dropped``: по общему не отличить переполнение буфера (транзиентно)
            # от вычистки шума (постоянно), а различие и есть смысл счётчика.
            "suppressed_probe": self._suppressed_probe,
            "suppressed_noise": self._suppressed_noise,
            # ``None`` — порога нет, пишется всё. Значение, а не пустая строка:
            # иначе «порог не задан» и «порог INFO» выглядели бы на баннере
            # одинаково, а это два разных ответа на вопрос «что отбрасывается».
            "min_level": self._min_level,
            "mirrored": self._mirrored,
            "mirror_failed": self._mirror_failed,
            "persist_session_events": self._persist,
        }


__all__ = [
    "ACCEPTED",
    "DROPPED",
    "EventSink",
    "EventWriter",
    "NO_SINK",
    "REJECTED",
    "SUPPRESSED",
]
