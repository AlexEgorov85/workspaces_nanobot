"""Консоль оператора: одна строка = один факт = одно имя события = два стока.

Модуль отвечает на единственный вопрос, ради которого оператор смотрит в
терминал: *этот запрос обработан и ответ ушёл человеку или ещё нет?*
До change в терминал писали три писателя на трёх языках (loguru без
формата, rich ``console.print`` в stdout, хук tool'ов), и ни один не отвечал
на него.

**Консоль — представление событий, а не вторая система печати.** Строка
порождается одной функцией :func:`render` из объекта события, и тот же объект
уходит в журнал. Ни отдельной шины, ни очереди, ни подписки, ни своего потока
здесь нет: единственная подписка — вызов из существующего пути события
(:meth:`DbLoggingService.log_event`, ``lib/services/db_logging_service.py``).

Три класса фактов, смешивать которые запрещено:

* **Класс А — запись есть в журнале.** Имя строки = ``event_type`` из
  канонического словаря (``mcp-platform/libs/enterprise_common/eventing/
  types.py`` → ``EVENT_TYPES``). ``grep`` в терминале равен SQL в журнале по
  одному и тому же факту. Словарь эта консоль НЕ расширяет.
* **Класс Б — записи в журнале нет.** Жизненный цикл задачи воркера и размер
  очереди. Носитель — существующий маркер ``TASK lifecycle``
  (``lib/channels/postgres_channel.py``) с уже существующими полями
  ``task=``/``phase=``/``chat=``. Имя вида ``task.*``/``worker.*``, похожее на
  имя события журнала, заводить запрещено: такой факт выдавал бы себя за
  событие, которого в базе нет.
* **Класс В — вердикт старта.** Строка помечена ``startup`` и в журнале тоже
  не значится ничего; это объявление о состоянии процесса, а не факт оборота.

**Личность берётся из объекта события.** ``who`` и ``task`` — явно
именованные поля, и они читаются из самого события (``LogEvent.who``/
``LogEvent.task``, с откатом на ``actor``/``request_id``/``session_id``, то
есть тоже на поля события). Сборка из окружения запрещена: иначе стоки
разъедутся. Ключ ``channel`` НЕ переиспользуется — в loguru у нанобота это
транспорт (``nanobot/channels/base.py``), и в журнале ``LogEvent.channel`` —
тоже транспорт. Ключ ``source`` занят в ``metadata.source`` журнала.

**Формат объявлен ровно здесь и ровно один раз** (:data:`LINE_FORMAT`).
``rich`` остаётся там, где ему место: таблицы и блочный баннер. Стартовый
инвентарный блок (``Hooks connected:``/``✓ <patch>``) — намеренное
исключение: его читает ``tools/diagnose_startup.py`` регулярками, привязанными
к началу строки, и префикс формата сломал бы разбор молча.

**Уровень объявляется одним ключом** ``gateway.console_level``
(``quiet|turn|trace``, дефолт ``turn``) и НЕ реализуется повышением уровня
loguru: иначе факт простоя, сегодня DEBUG, пришлось бы поднимать до DEBUG
целиком. Старые булевы ключи дают предупреждение со значением, которое из
них следует, — не тихий игнор.

Чего модуль не делает намеренно:

* не печатает текст ответа (у строки доставки есть задержка и размер);
* не принимает срабатывания защитника от повторов — ``runtime/anti-loop``
  запрещает для них прямой stdout/stderr, диагностика остаётся в журнале
  через ``event_type IN ('tool_repeat_blocked', 'tool_repeat_warned')``;
* не печатает периодическую heartbeat-строку простоя: у неё нет владельца
  состояния воркеров, и остаточная дыра объявлена в баннере, а не спрятана.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import ConfigurationError

#: Объявленная глубина вывода. Единственный ключ — ``gateway.console_level``.
CONSOLE_LEVEL_QUIET = "quiet"
CONSOLE_LEVEL_TURN = "turn"
CONSOLE_LEVEL_TRACE = "trace"

CONSOLE_LEVELS: tuple[str, ...] = (
    CONSOLE_LEVEL_QUIET,
    CONSOLE_LEVEL_TURN,
    CONSOLE_LEVEL_TRACE,
)

#: Ключ в конфиге. Объявлен здесь, а не продублирован в читателях.
CONSOLE_LEVEL_KEY = "console_level"

#: Дефолт — ``turn``. Прежний дефолт ``print_worker_activity=false`` означал,
#: что за оборотом при ``log_level=INFO`` не видно ничего, и это был дефект
#: наблюдаемости, а не настройка.
DEFAULT_CONSOLE_LEVEL = CONSOLE_LEVEL_TURN

#: Плейсхолдер пустой колонки. Колонка, которая иногда пуста, не отвечает ни
#: на какой вопрос — ровно тот дефект, который change закрывает.
PLACEHOLDER = "-"

#: ФОРМАТ ПОСТРОЧНОГО ВЫВОДА. Объявлен ОДИН РАЗ и только здесь; читатель и
#: писатель обязаны импортировать, а не собирать свой (``test_single_format_is
#: _declared_in_one_place``).
LINE_FORMAT = (
    "{time:HH:mm:ss.SSS} | {level: <5} | {extra[who]:<10} | "
    "{extra[task]:<24} | {message}"
)

#: Существующий маркер фактов класса Б. Взят из ``postgres_channel`` как есть:
#: свой маркер означал бы второе имя для одного и того же факта.
LIFECYCLE_MARKER = "TASK lifecycle"

#: Метка вердиктов старта (класс В). Это НЕ имя события журнала, и страж
#: ``test_console_event_names_come_from_the_journal_dictionary`` проверяет
#: именно эту разницу: имена классов А обязаны быть в словаре, ``startup``
#: и ``TASK lifecycle`` — обязаны отсутствовать.
STARTUP_MARKER = "startup"

#: Ключ в loguru-extra, которым факт помечает свою объявленную глубину.
#: По нему sink решает, печатать ли строку: это ЕДИНСТВЕННОЕ место отбора по
#: уровню, и оно не поднимает уровень loguru.
CONSOLE_FACT_EXTRA_KEY = "_console_depth"

#: Глубины, которые видны на каждом уровне (ошибки и предупреждения).
_ALWAYS_VISIBLE = (CONSOLE_LEVEL_QUIET,)

#: Префиксы имён, дающих глубину ``trace``: по одному факту на каждый вызов
#: инструмента и на каждый вызов модели.
_TRACE_PREFIXES = ("tool.", "llm.")

#: Уровни записи, которые видны всегда: ошибка проглотать нельзя, иначе
#: оператор увидит тишину вместо отказа.
_ALWAYS_LEVELS = frozenset({"ERROR", "CRITICAL", "WARNING", "WARN"})

#: Старые булевы ключи → уровень, который из их значения следует. Объявлено
#: для ПЕРЕХОДА, а не для чтения: снятые ключи остаются в чужих конфигах, и
#: молчаливый игнор изменил бы вывод у того, кто их выставил, без единого
#: слова. ``None`` — из значения уровня не следует (ключ уже ничего не читал).
LEGACY_FLAG_LEVELS: dict[str, dict[bool, str | None]] = {
    # Активность воркеров — факт оборота.
    "print_worker_activity": {True: CONSOLE_LEVEL_TURN, False: CONSOLE_LEVEL_QUIET},
    # По одному факту на вызов модели.
    "print_llm_calls": {True: CONSOLE_LEVEL_TRACE, False: CONSOLE_LEVEL_TURN},
    # Активность db-worker'ов — по одному факту на операцию пула.
    "print_db_activity": {True: CONSOLE_LEVEL_TRACE, False: CONSOLE_LEVEL_TURN},
    # Ключ уже ничего не читал (см. docstring ``TerminalToolPrintHook``):
    # перехода по нему нет, и выдумывать его — значит соврать оператору.
    "print_tools": {True: None, False: None},
}

_DEPTH_RANK = {name: rank for rank, name in enumerate(CONSOLE_LEVELS)}

#: Действующая глубина процесса. Ставится :func:`set_console_level` из
#: ``configure_loguru``; sink читает её на каждой записи, поэтому переключение
#: уровня не требует переустановки sink'а.
_effective_level: str = DEFAULT_CONSOLE_LEVEL

#: Предупреждения о старых булевых ключах, собранные при настройке вывода.
#: Печатает их баннер (``ApplicationContext.start``): на шаге настройки sink
#: только что поставлен, и объявлять о настройке из настройки — значит
#: зависеть от того, успел ли он встать.
_pending_legacy_warnings: list[str] = []


def set_legacy_warnings(warnings: list[str]) -> None:
    """Сохранить предупреждения о старых ключах для баннера."""
    global _pending_legacy_warnings
    _pending_legacy_warnings = list(warnings or [])


def pending_legacy_warnings() -> list[str]:
    """Предупреждения о старых булевых ключах, собранные при настройке."""
    return list(_pending_legacy_warnings)


def normalize_console_level(value: Any) -> str:
    """Проверить значение глубины и вернуть каноническое имя.

    Неизвестное значение отвергается объявленной ошибкой, а не заменяется
    дефолтом молча: молчаливая замена означала бы, что оператор объявил одно,
    а получил другое, и узнал бы об этом из молчания консоли.

    Raises:
        ConfigurationError: значение вне ``quiet|turn|trace``.
    """
    text = str(value if value is not None else "").strip()
    if text in CONSOLE_LEVELS:
        return text
    raise ConfigurationError(
        f"gateway.{CONSOLE_LEVEL_KEY}={value!r} — недопустимая глубина вывода. "
        f"Допустимо: {' | '.join(CONSOLE_LEVELS)}. "
        f"Глубина объявляется ОДНИМ ключом: четыре булева флага "
        f"({', '.join(sorted(LEGACY_FLAG_LEVELS))}) больше не выбирают вывод."
    )


def console_level_of(gateway_settings: dict[str, Any] | None) -> str:
    """Прочитать ``gateway.console_level`` из секции ``gateway``.

    Отсутствие ключа — не ошибка: дефолт объявлен один раз
    (:data:`DEFAULT_CONSOLE_LEVEL`). Неверное значение — ошибка.
    """
    settings = gateway_settings or {}
    if CONSOLE_LEVEL_KEY not in settings:
        return DEFAULT_CONSOLE_LEVEL
    return normalize_console_level(settings.get(CONSOLE_LEVEL_KEY))


def legacy_flag_warnings(gateway_settings: dict[str, Any] | None) -> list[str]:
    """Предупреждения о старых булевых ключах с уровнем, который из них следует.

    Пустой список — в конфиге старых ключей нет (обычный случай после перехода).

    Returns:
        По строке на каждый оставшийся старый ключ. Ключ с ``None`` получает
        предупреждение без уровня: из его значения уровень не следует, и
        называть уровень там — значит выдумать его.
    """
    settings = gateway_settings or {}
    warnings: list[str] = []
    for key, mapping in LEGACY_FLAG_LEVELS.items():
        if key not in settings:
            continue
        raw = settings.get(key)
        level = mapping.get(bool(raw))
        if level is None:
            warnings.append(
                f"gateway.{key}={raw!r} больше не читается и на "
                f"gateway.{CONSOLE_LEVEL_KEY} не отображается: ключ не имел "
                f"потребителей ещё до перехода. Глубина вывода объявляется "
                f"ключом gateway.{CONSOLE_LEVEL_KEY} "
                f"({' | '.join(CONSOLE_LEVELS)})."
            )
            continue
        warnings.append(
            f"gateway.{key}={raw!r} больше не читается как флаг: тот же вывод "
            f"даёт gateway.{CONSOLE_LEVEL_KEY}={level}. Ключ оставлен в "
            f"конфиге — уберите его, иначе при следующем изменении уровня он "
            f"начнёт описывать уже не тот вывод."
        )
    return warnings


def set_console_level(level: Any) -> str:
    """Объявить действующую глубину вывода для процесса.

    Вызывается из :func:`lib.utils.logging_utils.configure_loguru` — то есть
    однажды на точке старта, а не на каждой записи.
    """
    global _effective_level
    _effective_level = normalize_console_level(level)
    return _effective_level


def effective_console_level() -> str:
    """Действующая глубина вывода (для баннера и для стражей)."""
    return _effective_level


def depth_visible(depth: str) -> bool:
    """Видна ли глубина ``depth`` при действующем уровне.

    Сравнение рангов, а не вхождение в список: уровней всего три, и правило
    «глубже — значит громче» обязано выполняться само собой.
    """
    wanted = _DEPTH_RANK.get(depth)
    current = _DEPTH_RANK.get(_effective_level)
    if wanted is None or current is None:
        return False
    return current >= wanted


def required_depth(event_type: str, level: str | None) -> str:
    """Объявленная глубина факта по его имени и уровню записи.

    Имя события решает глубину, а не уровень loguru: именно поэтому факт
    простоя (сегодня DEBUG) может быть фактом ``turn`` и не тащить за собой
    весь DEBUG-шум.
    """
    if str(level or "").upper() in _ALWAYS_LEVELS:
        return CONSOLE_LEVEL_QUIET
    name = str(event_type or "")
    if name.startswith(_TRACE_PREFIXES):
        return CONSOLE_LEVEL_TRACE
    return CONSOLE_LEVEL_TURN


def _loglevel_of(fact: ConsoleFact) -> str:
    """Уровень записи loguru для факта.

    ``WARN`` у loguru нет (есть только ``WARNING``), а объявление уровня
    предупреждения на уровне консоли пишется как ``WARN`` — приводится здесь,
    иначе факт молча ронял бы запись значением, которого у loguru нет.
    """
    name = str(fact.event_level or "INFO").upper()
    return {"WARN": "WARNING"}.get(name, name)


@dataclass
class ConsoleFact:
    """Одна консольная строка как объект.

    Один объект — оба стока. Поле :attr:`event` держит исходное ``LogEvent``
    для класса А: журнал пишет его же, и консоль не имеет права собирать
    содержимое строки из чего-то другого.
    """

    marker: str
    detail: str = ""
    who: str | None = None
    task: str | None = None
    depth: str = CONSOLE_LEVEL_TURN
    event_level: str = "INFO"
    event: Any | None = None


def _clean(value: Any, limit: int = 40) -> str:
    """Односрочное безопасное значение поля.

    Хвост длинных идентификаторов обрезается слева: в идентификаторе
    различающей обычно правая часть.
    """
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        return ""
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def _size_of(text: Any) -> str:
    """Размер текста В СИМВОЛАХ — без самого текста."""
    try:
        return f"размер={len(str(text or ''))}симв"
    except Exception:  # noqa: BLE001 - факт не должен роняться о тип
        return "размер=?"


def _detail_for(event: Any) -> str:
    """Человеческая часть строки из полей САМОГО события.

    Содержимое ответа и текст запроса сюда не попадают ни при каком имени
    события: для фактов с текстом выводится размер. Это единственное правило
    на весь модуль, а не решение по каждому имени.
    """
    name = str(getattr(event, "event_type", "") or "")
    payload = getattr(event, "payload", None) or {}
    meta = getattr(event, "metadata", None) or {}
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(meta, dict):
        meta = {}

    def _number(source: dict, key: str) -> str:
        value = source.get(key)
        return f"{value}мс" if isinstance(value, (int, float)) else "?"

    if name == "agent.received":
        # ``канал=`` без значения — та же пустая колонка, только внутри
        # строки: читается как «канал неизвестен», хотя неизвестно лишь
        # потому, что событие его не заполнило. Плейсолдер честнее.
        channel = _clean(getattr(event, "channel", ""), 16) or PLACEHOLDER
        return f"канал={channel} " + _size_of(payload.get("content"))
    if name in ("agent.delivered", "agent.responded"):
        latency = meta.get("latency_ms")
        parts = []
        if isinstance(latency, (int, float)):
            parts.append(f"задержка={latency}мс")
        tokens = meta.get("tokens_used")
        if isinstance(tokens, (int, float)):
            parts.append(f"токены={int(tokens)}")
        content = payload.get("content")
        if content is None:
            content = payload.get("final_content")
        parts.append(_size_of(content))
        return " ".join(parts)
    if name in ("agent.completed", "agent.failed"):
        outcome = payload.get("outcome") or payload.get("status")
        return "исход=" + _clean(outcome or "?", 24) + " " + (
            f"задержка={payload.get('latency_ms')}мс"
            if payload.get("latency_ms") is not None
            else "задержка=?"
        )
    if name.startswith("tool."):
        status = payload.get("status") or getattr(event, "name", "") or "?"
        return (
            f"инструмент={_clean(getattr(event, 'name', '') or '?', 40)} "
            f"статус={_clean(status, 16)} "
            f"длительность={_number(meta, 'latency_ms')}"
        )
    if name.startswith("llm."):
        model = meta.get("model") or getattr(event, "name", "") or "?"
        iteration = meta.get("iteration")
        return (
            f"модель={_clean(model, 32)} "
            f"итерация={iteration if iteration is not None else '?'}"
        )
    # Имя, для которого правило не объявлено, даёт строку без подробностей:
    # печатать ``summary`` «на всякий случай» означало бы вываливать в
    # терминал содержимое, о котором никто не решал.
    return ""


def fact_from_event(event: Any) -> ConsoleFact:
    """Собрать факт консоли из объекта события журнала.

    Личность берётся ИЗ СОБЫТИЯ и только из него: ``LogEvent.who``/``.task``,
    а если писатель их не проставил — ``actor``/``request_id``/``session_id``,
    то есть тоже поля события. Сборка из окружения (thread-local, current
    session, модуль-глобал) запрещена: иначе стоки разъедутся именно там,
    где консоль должна была бы их сойтись.
    """
    task = (
        getattr(event, "task", None)
        or getattr(event, "request_id", None)
        or getattr(event, "session_id", None)
    )
    who = getattr(event, "who", None) or getattr(event, "actor", None)
    event_level = str(getattr(event, "level", "INFO") or "INFO")
    return ConsoleFact(
        marker=str(getattr(event, "event_type", "") or ""),
        detail=_detail_for(event),
        who=_clean(who),
        task=_clean(task),
        depth=required_depth(getattr(event, "event_type", ""), event_level),
        event_level=event_level,
        event=event,
    )


def worker_fact(
    *,
    worker: str,
    phase: str,
    task: str | None = None,
    chat: str | None = None,
    detail: str = "",
    extra: str = "",
) -> ConsoleFact:
    """Факт класса Б: жизненный цикл задачи воркера и размер очереди.

    Записи в журнале у него нет, поэтому носитель — существующий маркер
    ``TASK lifecycle`` с полями ``task=``/``phase=``/``chat=``, а не имя вида
    ``task.*``: тот выдавал бы факт за событие журнала, которого в базе нет.
    """
    parts = [
        f"task={_clean(task) or PLACEHOLDER}",
        f"phase={_clean(phase) or PLACEHOLDER}",
    ]
    if chat:
        parts.append(f"chat={_clean(chat, 32)}")
    if extra:
        parts.append(extra)
    if detail:
        parts.append(_clean(detail, 120))
    return ConsoleFact(
        marker=LIFECYCLE_MARKER,
        detail=" ".join(parts),
        who=_clean(worker),
        task=_clean(task) or _clean(chat),
        depth=CONSOLE_LEVEL_TURN,
        event_level="INFO",
    )


def startup_fact(text: str, *, who: str = "gateway", level: str = "INFO") -> ConsoleFact:
    """Вердиктная строка старта (класс В): тот же формат, тот же поток."""
    return ConsoleFact(
        marker=STARTUP_MARKER,
        detail=str(text),
        who=_clean(who),
        task=PLACEHOLDER,
        depth=CONSOLE_LEVEL_QUIET,
        event_level=level,
    )


def render(fact: ConsoleFact) -> str:
    """ЕДИНСТВЕННАЯ функция рендера построчной строки консоли.

    Один объект на входе, одна строка на выходе; печатает её :func:`emit`.
    Формат колонок — :data:`LINE_FORMAT`, и он объявлен один раз.
    """
    detail = str(fact.detail or "").strip()
    if not detail:
        return fact.marker
    return f"{fact.marker} · {detail}"


def emit(fact: ConsoleFact) -> None:
    """Напечатать факт консоли, привязав исполнителя и задачу.

    Писатель факта (хук, канал, подписчик) сюда отдаёт объект и больше ничего
    не печатает: ни ``console.print``, ни ``print`` для построчного факта.
    Отбор по глубине делает sink (см.
    :data:`CONSOLE_FACT_EXTRA_KEY`) — это единственное место отбора, и оно
    не поднимает уровень loguru.
    """
    from loguru import logger

    logger.bind(
        who=_clean(fact.who) or PLACEHOLDER,
        task=_clean(fact.task) or PLACEHOLDER,
        **{CONSOLE_FACT_EXTRA_KEY: fact.depth},
    ).log(_loglevel_of(fact), render(fact))


def emit_event(event: Any) -> bool:
    """Отдать событие журнала консоли: второй сток того же объекта.

    Вызывается из :meth:`DbLoggingService.log_event` сразу после успешной
    постановки события в очередь журнала. Возвращает ``True``, если строка
    напечатана.

    Напечатать раньше, чем событие принято журналом, нельзя: тогда строка в
    консоли заменяла бы запись в журнале, а это прямо запрещено.
    """
    fact = fact_from_event(event)
    if not depth_visible(fact.depth):
        return False
    emit(fact)
    return True
