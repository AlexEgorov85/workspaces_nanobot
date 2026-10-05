"""Словарь типов событий.

Строка типа приходит из кода, поэтому словарь закрытый: ``agent.completed`` и
``mcp_history_search_done`` — не одно и то же, но выглядят одинаково, и через полгода
в журнале будет пять похожих имён одного события. Проверка на входе дешевле, чем
разбор журнала постфактум.

Префикс отвечает за источник ответственности, а не за процесс: ``tool.*``
порождается и агентом, и платформой, а различаются они полем ``component``.

Зарезервированный MCP префикс ``io.modelcontextprotocol/`` в словарь не входит и
не должен: это служебный обмен протокола, а не события проекта.
"""

from __future__ import annotations

#: События агента.
AGENT_STARTED = "agent.started"
AGENT_COMPLETED = "agent.completed"
AGENT_FAILED = "agent.failed"

#: Остальные точки оборота. Объявлены вместе с ``AGENT_STARTED``, потому что
#: принадлежат той же шкале времени оборота, а не отдельной подсистеме:
#: ``received`` — вход, ``responded`` — ответ сформирован, ``delivered`` —
#: отдан каналу, ``compacted`` — контекст ужат, ``degraded`` — оборот жив, но
#: что-то отказало. Пять разных фактов, пять разных имён: сведение их в одно
#: имя означало бы потерю различия, и по таблице нельзя было бы сказать, дошёл
#: ли ответ до пользователя.
AGENT_RECEIVED = "agent.received"
AGENT_RESPONDED = "agent.responded"
AGENT_DELIVERED = "agent.delivered"
AGENT_COMPACTED = "agent.compacted"
AGENT_DEGRADED = "agent.degraded"

#: Обращения к модели.
LLM_REQUESTED = "llm.requested"
LLM_COMPLETED = "llm.completed"
LLM_FAILED = "llm.failed"

#: Обмен телами с моделью за итерацию. Отдельное имя от ``llm.requested``/
#: ``llm.completed`` не избыточностью, а носителем: только здесь лежат полные
#: тела запроса и ответа, которых больше нигде в журнале нет. Считать длительностью
#: оборота не по нему, а по паре границ.
LLM_EXCHANGED = "llm.exchanged"

#: Исполнение операций. Их порождает конвейер исполнения (§ runtime/tool-execution).
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"
TOOL_TIMEOUT = "tool.timeout"

#: Вызов, который не состоялся по вине самого вызова (повтор под защитником).
#: Отличать от ``tool.failed`` обязательно: там исполнение было и упало, а
#: здесь его не было вовсе — сводить их в одно имя значило бы считать отказы
#: защитника ошибками исполнения.
TOOL_SUPPRESSED = "tool.suppressed"

#: Вложения.
ARTIFACT_CREATED = "artifact.created"
ARTIFACT_READ = "artifact.read"

#: Проверки качества.
QUALITY_CHECK = "quality.check"

#: Шаги разбора юридического документа (операция ``analyze_document``).
#: Пять имён, а не одно с полем ``phase``: журнал читают по имени события, и
#: один конвейерный шаг с признаком внутри не отличить от другого по выборке
#: имён. Набор — по фактам, которые между собой не сводимы: начало, явное
#: подтверждение, частичное завершение, финальное завершение, отказ. Отказ по
#: потолку вызова (``ExecutionTimeout``) конвейер пишет сам своим
#: ``tool.failed`` с ``status: "timeout"``, поэтому отдельного имени для него
#: здесь нет и не требуется — это требование распространяется на шаги самой
#: операции, а не заменяет журнал конвейера.
#:
#: Объявлены **до** первой записи: политика неизвестных имён на живом контуре —
#: ``strict`` (``platform.json → data.log_unknown_event_type_policy``), то есть
#: необъявленное имя отказывает вызов, а не пишется втихую. Объявлять по факту
#: первой записи поздно: запись уже отказала, и объявление её не воскресит.
LEGAL_ANALYSIS_STARTED = "legal_analysis_step"
LEGAL_ANALYSIS_CONFIRMED = "legal_analysis_confirmation"
LEGAL_ANALYSIS_COMPLETED = "legal_analysis_completed"
LEGAL_ANALYSIS_PARTIAL = "legal_analysis_partial"
LEGAL_ANALYSIS_REFUSED = "legal_analysis_refused"

#: Полный словарь. Порядок — по источнику ответственности, не по алфавиту.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        AGENT_STARTED,
        AGENT_COMPLETED,
        AGENT_FAILED,
        AGENT_RECEIVED,
        AGENT_RESPONDED,
        AGENT_DELIVERED,
        AGENT_COMPACTED,
        AGENT_DEGRADED,
        LLM_REQUESTED,
        LLM_COMPLETED,
        LLM_FAILED,
        LLM_EXCHANGED,
        TOOL_STARTED,
        TOOL_COMPLETED,
        TOOL_FAILED,
        TOOL_TIMEOUT,
        TOOL_SUPPRESSED,
        ARTIFACT_CREATED,
        ARTIFACT_READ,
        QUALITY_CHECK,
        LEGAL_ANALYSIS_STARTED,
        LEGAL_ANALYSIS_CONFIRMED,
        LEGAL_ANALYSIS_COMPLETED,
        LEGAL_ANALYSIS_PARTIAL,
        LEGAL_ANALYSIS_REFUSED,
    }
)

#: Префиксы допустимых типов. Отдельный список, чтобы опечатка в последнем
#: компоненте имени (``tool.complted``) не прошла проверку по полному совпадению
#: и не плодила новый тип в словаре.
ALLOWED_PREFIXES: tuple[str, ...] = ("agent.", "llm.", "tool.", "artifact.", "quality.")


#: Имена пробного характера. Ровно те, что перечислены в требовании
#: «Пробные события не пишутся в продовую таблицу»: префиксы ``smoke.`` и
#: ``probe_`` и имя ``live.db_probe``.
#:
#: Список объявлен здесь, а не у каждой проверки, потому что проверка одна:
#: и писатель, и страж, и будущий запрет на пути ``log_events`` обязаны
#: отвечать на один и тот же вопрос одинаково. Добавление сюда нового имени
#: автоматически закрывает его на всех путях, а не в том модуле, где про него
#: вспомнили.
PROBE_EVENT_PREFIXES: tuple[str, ...] = ("smoke.", "probe_")
PROBE_EVENT_NAMES: frozenset[str] = frozenset({"live.db_probe"})


def is_probe_event_type(event_type: str) -> bool:
    """Пробное ли это имя события.

    Повторяет условие замера из требования — ``event_type LIKE 'smoke.%' OR
    event_type LIKE 'probe_%' OR event_type = 'live.db_probe'``. Проверка по
    регистру нечувствительна: имя приходит извне, и ``SMOKE.x`` отличается от
    ``smoke.x`` только написанием, а в таблице это один и тот же мусор.
    """
    value = str(event_type or "").strip().lower()
    if value in PROBE_EVENT_NAMES:
        return True
    return any(value.startswith(prefix) for prefix in PROBE_EVENT_PREFIXES)


class UnknownEventType(ValueError):
    """Тип события вне словаря."""

    code = "invalid_params"


def is_known(event_type: str) -> bool:
    return str(event_type or "") in EVENT_TYPES


def is_declared_prefix(event_type: str) -> bool:
    """Префикс имени объявлен в :data:`ALLOWED_PREFIXES`, а само имя — нет.

    Список префиксов объявлен именно для такого вопроса: он отделяет опечатку
    в последнем компоненте (``tool.complted`` — человек уже в правильном
    соглашении, поправлять нужно одно имя) от чужой схемы имён целиком
    (``tool_call`` — здесь нужно менять соглашение, а не слово). Поэтому он
    используется на пути ``log_events`` (``DataService._observe_event_type``),
    где имя приходит извне, иначе объявление осталось бы просто текстом.
    """
    value = str(event_type or "")
    return any(value.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def require_known(event_type: str) -> str:
    """Проверить тип и вернуть его же.

    Бросает, а не пропускает молча: событие с неизвестным типом в журнале
    нечитаемо, и обнаружится это через месяц в запросе, где его уже не разобрать.
    """
    value = str(event_type or "").strip()
    if value not in EVENT_TYPES:
        raise UnknownEventType(
            f"тип события {value!r} вне словаря; допустимы: "
            + ", ".join(sorted(EVENT_TYPES))
        )
    return value
