"""Защитник от вырожденных циклов одинаковых tool-вызовов.

Навык ``RepeatGuardHook`` — framework-хук уровня агента. Накапливает
последние ``window_size`` вызовов оборота и, когда одна и та же пара
``(tool_name, canonical_args)`` встречается ``max_repeats_in_window`` раз
подряд внутри окна, действует по режиму:

* ``off``   — ничего не делает (дефолт, полная обратная совместимость);
* ``warn``  — ровно одно событие ``tool.suppressed`` в журнал
  (``payload["mode"]="warn"``);
* ``block`` — вызов подменяется синтетической ошибкой, ровно одно событие
  ``tool.suppressed`` (``payload["mode"]="block"``).

**Почему ``raise``, а не возврат значения.** Hook-API nanobot не имеет
«мягкого» способа отклонить вызов: у ``AgentHook`` нет возвращаемого
значения, которое читает раннер. Единственный доступный канал — исключение
из ``before_execute_tool``. Но и тут два тупика, оба найдены на живом коде
nanobot 0.3.5, а не в документации:

1. ``HookRegistry._for_each_hook_safe`` (``nanobot/agent/hook.py:174-183``)
   глотает исключение каждого хука и логирует его. Без ``reraise=True``
   блокировка была бы **молчаливым no-op**: состояние обновилось бы,
   а вызов всё равно выполнился.
2. С ``reraise=True`` исключение пробрасывается, но вызывается оно на
   ``tools/execution.py:165`` — **вне** ``try``-блока, который начинается
   строкой 166. Поэтому «синтетический результат ``Error: ...``», который
   обещает design change'а, не образуется: исключение уходит в
   ``execute_tool_calls`` и выше, и весь оборот падает.

Поэтому хук создаётся с ``reraise=True`` и поднимает
``RepeatGuardBlocked``; его перехватывает патч
``RuntimePatcher.patch_repeat_guard_block``, который возвращает настоящий
синтетический tool-результат и вызывает ``on_execute_tool_error`` — так
модель видит обычную ошибку инструмента и может попробовать иначе, вместо
того чтобы потерять весь оборот.

Сброс состояния — в ``before_iteration``, а не в ``before_run``: у
``AgentRunHookContext`` в nanobot 0.3.5 **нет** поля ``session_key``
(его создаёт ``runner.py:311`` как ``AgentRunHookContext(messages=...)``),
поэтому сброс по ключу там физически невозможен, а глобальный сброс на
каждом ``before_run`` стирал бы буфер у параллельных оборотов. У
``AgentHookContext`` ключ есть (``runner.py:436-440``), а ``iteration == 0``
однозначно означает первый вызов нового оборота.

См. ``openspec/specs/runtime/anti-loop/spec.md``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import time
from collections import deque
from pathlib import PurePath
from typing import Any

from loguru import logger
from nanobot.agent import AgentHook

#: Ключ-«bucket» для оборотов без session_key (прямые SDK-вызовы).
_DEFAULT_KEY = ""

#: Маркер, которым помечается вызов, чьи аргументы не удалось привести к
#: детерминированному виду. Такой вызов пропускается: сравнивать его не с чем,
#: а молча считать «не повтором» значит спрятать настоящий цикл.
_SKIP = object()

#: Верхняя граница числа одновременно отслеживаемых сессий. Без неё dict
#: рос бы бесконечно на долгоживущем gateway; LRU по времени последнего
#: касания даёт предсказуемую границу памяти.
_MAX_TRACKED_SESSIONS = 512


def _stable_fallback(value: Any) -> Any:
    """Детерминированное строковое представление несериализуемого значения.

    Вызывается ``json.dumps(..., default=...)`` только для типов, которых
    json не знает. Порядок проверок важен: сначала то, что имеет
    каноническое представление по определению.

    Для произвольных объектов берётся ``str()``; если он выглядит
    недетерминированным (содержит адрес вида ``0x7f…``), возвращается
    ``_SKIP`` — вызов исключается из проверки целиком.
    """
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, bytes):
        # Длина входит в представление: одни и те же байты разной длины —
        # разные аргументы.
        return f"<bytes:{len(value)}:{value.hex()}>"
    if isinstance(value, bytearray):
        return f"<bytes:{len(value)}:{bytes(value).hex()}>"
    if isinstance(value, (set, frozenset)):
        # Множества не упорядочены: сортируем, иначе {1,2} и {2,1} дали бы
        # разные fingerprint'ы.
        return ["<set>", *(str(v) for v in sorted(value, key=repr))]
    if isinstance(value, float) and not math.isfinite(value):
        return f"<float:{value!r}>"
    try:
        text = str(value)
    except Exception:
        return _SKIP
    if "0x" in text and any(c in text for c in "0123456789abcdef"):
        # Похоже на repr с адресом объекта — детерминизма нет.
        return _SKIP
    return text


def _canonical_arguments(arguments: Any) -> str | object:
    """Канонический JSON аргументов либо ``_SKIP``, если он недетерминирован.

    ``sort_keys`` действует на всех уровнях вложенности, включая dict внутри
    list, поэтому ``{"b":2,"a":1}``, ``{"a":1,"b":2}`` и
    ``{"query":{"b":2,"a":1}}`` против ``{"query":{"a":1,"b":2}}`` дают
    одно представление.
    """
    try:
        return json.dumps(
            arguments,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=_stable_fallback,
        )
    except ValueError:
        # NaN/Inf: json их не принимает, и это осознанно — такое значение
        # не имеет канонического представления.
        return _SKIP
    except TypeError:
        return _SKIP


def _fingerprint_hash(tool_name: str, canonical: str) -> str:
    """8-символьный hex для журнала.

    Участвует только в observability: детекция идёт по точному равенству
    канонических строк, поэтому коллизия этого хэша не может привести ни к
    ложному срабатыванию, ни к пропуску.
    """
    return hashlib.blake2b(
        f"{tool_name}\x00{canonical}".encode(),
        digest_size=4,
    ).hexdigest()


class RepeatGuardBlocked(RuntimeError):
    """Вызов отклонён защитником в режиме ``block``.

    Несёт контекст вызова, чтобы патч мог построить синтетический
    tool-результат и вызвать ``on_execute_tool_error`` — так же, как это
    сделал бы сам инструмент, упади он с ошибкой.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Any = None,
        tool_call: Any = None,
        tool: Any = None,
        params: Any = None,
    ) -> None:
        super().__init__(message)
        self.context = context
        self.tool_call = tool_call
        self.tool = tool
        self.params = params


class RepeatGuardHook(AgentHook):
    """Скользящее окно последних вызовов оборота + детект повторов.

    Один экземпляр делится между оборотами, и обслуживать они могут
    конкурентно, поэтому всё состояние изолировано по ``session_key``.
    """

    def __init__(
        self,
        settings: Any,
        db_logging_service: Any = None,
    ) -> None:
        """Создать защитник.

        Args:
            settings: ``GatewayRepeatGuardSettings`` либо ``None``
                (трактуется как ``mode="off"``).
            db_logging_service: ``DbLoggingService`` или ``None``.
                ``None`` не отключает детектор — только журнал.
        """
        # reraise=True обязателен: иначе HookRegistry._for_each_hook_safe
        # проглотит RepeatGuardBlocked, и режим block станет no-op, который
        # при этом выглядит работающим.
        super().__init__(reraise=True)
        self._mode = str(getattr(settings, "mode", "off") or "off")
        self._window_size = int(getattr(settings, "window_size", 20) or 20)
        self._max_repeats = int(
            getattr(settings, "max_repeats_in_window", 3) or 3
        )
        raw_exempt = getattr(settings, "exempt_tools", None) or []
        self._exempt: frozenset[str] = frozenset(str(x) for x in raw_exempt)
        self._db = db_logging_service
        self._state: dict[str, dict[str, Any]] = {}

    # -- служебное ---------------------------------------------------------

    @staticmethod
    def _bucket_key(ctx: Any) -> str:
        key = getattr(ctx, "session_key", None)
        return key if isinstance(key, str) else _DEFAULT_KEY

    def _bucket(self, key: str) -> dict[str, Any]:
        bucket = self._state.get(key)
        if bucket is None:
            bucket = {
                "calls": deque(maxlen=self._window_size),
                "published": set(),
                "touched": time.monotonic(),
            }
            self._state[key] = bucket
            self._evict_sessions()
        bucket["touched"] = time.monotonic()
        return bucket

    def _evict_sessions(self) -> None:
        """Ограничить число отслеживаемых сессий, выкидывая наименее свежие.

        ``after_run`` не знает ``session_key`` (см. module docstring), поэтому
        чистить по завершении оборота нельзя. Вместо этого держим жёсткий
        потолок: на gateway с тысячами сессий память остаётся ограниченной.
        """
        if len(self._state) <= _MAX_TRACKED_SESSIONS:
            return
        for key in sorted(
            self._state, key=lambda k: self._state[k]["touched"]
        )[: len(self._state) - _MAX_TRACKED_SESSIONS]:
            self._state.pop(key, None)

    def _summary(self, tool_name: str, count: int) -> str:
        return (
            f"repeat-guard: {count} identical {tool_name} calls in last "
            f"{self._window_size} iterations; use existing result or vary "
            "arguments"
        )

    def _publish(
        self,
        *,
        context: Any,
        tool_name: str,
        canonical: str,
        attempt: int,
        event_type: str,
        summary: str,
    ) -> None:
        """Одно событие в журнал. Ошибки не влияют на детект.

        Producer-контракт: enqueue + результат, а не гарантия появления в
        БД. Поэтому любой сбой здесь глотается с WARNING — детектор обязан
        работать и при недоступном журнале.
        """
        try:
            from lib.services.db_logging_service import LogEvent, try_log_event

            event = LogEvent(
                event_type=event_type,
                # ``WARN``, а не ``WARNING``: CHECK ``valid_level`` в
                # ``sql/logs/create_public_agent_gateway_logs.sql:30`` знает
                # ровно четыре написания, синонима среди них нет, а отказ по
                # уровню уносит весь батч, а не это событие.
                level="WARN",
                session_id=getattr(context, "session_key", None),
                actor="RepeatGuardHook",
                name="agent",
                summary=summary,
                payload={
                    "tool": tool_name,
                    "fingerprint_hash": _fingerprint_hash(tool_name, canonical),
                    "attempt": attempt,
                    "window_size": self._window_size,
                    "max_repeats_in_window": self._max_repeats,
                    "mode": self._mode,
                },
            )
            try_log_event(
                self._db, event, producer="RepeatGuardHook", event_type=event_type
            )
        except Exception as exc:  # noqa: BLE001 - fail-soft по контракту спеки
            logger.warning(
                "repeat-guard: не удалось записать событие {} для {}: {}",
                event_type,
                tool_name,
                exc,
            )

    # -- lifecycle ---------------------------------------------------------

    async def before_iteration(self, context: Any) -> None:
        """Сбросить state сессии на первом вызове нового оборота.

        Именно здесь, а не в ``before_run``: ``AgentRunHookContext`` не
        содержит ``session_key``, поэтому адресный сброс там невозможен, а
        глобальный сброс стирал бы буфер параллельных оборотов.
        ``AgentHookContext`` несёт и ключ, и номер итерации.
        """
        if self._mode == "off":
            return
        if getattr(context, "iteration", 0) != 0:
            return
        self._state.pop(self._bucket_key(context), None)

    async def before_execute_tool(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        """Посчитать вызов; при превышении порога — событие и/или блокировка."""
        if self._mode == "off":
            return
        tool_name = str(getattr(tool_call, "name", "") or "")
        if not tool_name or tool_name in self._exempt:
            return

        arguments = getattr(tool_call, "arguments", None)
        if arguments is None:
            arguments = params
        canonical = _canonical_arguments(arguments)
        if canonical is _SKIP:
            # Не с чем сравнивать — пропускаем вызов, а не считаем его
            # «не повтором»: молчаливый пропуск скрыл бы настоящий цикл.
            logger.debug(
                "repeat-guard: аргументы {} не приведены к детерминированному"
                " виду, вызов пропущен",
                tool_name,
            )
            return

        bucket = self._bucket(self._bucket_key(context))
        calls = bucket["calls"]
        published = bucket["published"]
        calls.append((tool_name, canonical))

        # Текущий вызов уже лежит в calls, поэтому sum() и есть «сколько раз
        # эта пара встретилась в окне» — добавлять 1 второй раз нельзя, иначе
        # порог срабатывал бы на max_repeats - 1.
        count = sum(1 for name, canon in calls if name == tool_name and canon == canonical)
        if count < self._max_repeats:
            return

        fingerprint = (tool_name, canonical)
        if fingerprint in published:
            # Уже сообщали про этот fingerprint в текущем окне: повторных
            # событий не пишем, иначе зациклившийся агент зальёт журнал.
            if self._mode == "block":
                raise RepeatGuardBlocked(
                    self._summary(tool_name, count),
                    context=context,
                    tool_call=tool_call,
                    tool=tool,
                    params=params,
                )
            return

        published.add(fingerprint)
        summary = self._summary(tool_name, count)
        # Одно имя на оба режима: различие не потеряно — оно в
        # ``payload["mode"]`` (``warn``/``block``) и в том, что при ``block``
        # вызов ниже заменяется синтетической ошибкой. Два имени ради одного
        # факта держали в словаре платформы два лишних слова, которых там нет.
        self._publish(
            context=context,
            tool_name=tool_name,
            canonical=canonical,
            attempt=count,
            event_type="tool.suppressed",
            summary=summary,
        )
        # summary уже начинается с "repeat-guard:" — второй префикс был бы
        # мусором в журнале и в логе одновременно.
        logger.warning("{}", summary)

        if self._mode == "block":
            raise RepeatGuardBlocked(
                summary,
                context=context,
                tool_call=tool_call,
                tool=tool,
                params=params,
            )

    async def on_execute_tool_error(
        self,
        context: Any,
        tool_call: Any,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        """No-op: детектор работает до вызова, состояние уже учтено."""
        return None

    async def after_run(self, context: Any) -> None:
        """Подчистить вытесненные fingerprints.

        Само состояние по ``session_key`` здесь удалить нельзя — в этом
        контексте ключа нет. Зато можно убрать из ``published`` fingerprint'ы,
        которые уже выпали из окна: иначе вернувшийся в окно вызов не
        опубликует событие, хотя для оператора это снова crossing.
        """
        for bucket in self._state.values():
            live = {(name, canon) for name, canon in bucket["calls"]}
            bucket["published"] &= live
