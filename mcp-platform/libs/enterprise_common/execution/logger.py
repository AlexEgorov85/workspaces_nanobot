"""Журнал одного вызова операции.

Слой не пишет в базу — он собирает события. Записью занимается
:class:`EventWriter`, а он один на процесс, и второй писатель здесь означал бы
возврат к журналу, который пишут трое и читают как один.

Правило о payload. В журнал уходят **ограниченные выдержки** аргументов и
результата плюс их размеры и хеши. Раньше были только размеры и хеши, и по
журналу нельзя было понять, о чём был вызов: инцидент требовал повторить его
вручную. Теперь тело видно на ограниченном префиксе, но не целиком — журнал
читают через ``history_search``, и полный ответ каждой операции в нём означал бы
дублирование, из-за которого `payload` разрастается до размера самой операции.

Поля выдержки берутся по **белому списку** политики: белый список, а не чёрный,
потому что новый параметр операции не должен молча начинать писаться в журнал.
Потолок применяется к выдержке целиком, а не к одному полю, — иначе суммарный
объём рос бы числом полей и перестал бы быть ограниченным.

Усечение и маскирование **видны** в payload: маркер усечения и число
замаскированных значений. Неотличимо усечённое тело от целого — ложь того же
класса, что и отсутствие строки об отказе.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..eventing.models import (
    COMPONENT_TOOL_EXECUTION,
    DIAGNOSTIC_LEVEL,
    SOURCE_ENTERPRISE_MCP,
    AgentEvent,
)
from ..eventing.types import (
    ARTIFACT_CREATED,
    QUALITY_CHECK,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    TOOL_TIMEOUT,
)
from ..session.artifact_store import Artifact
from .context import ToolExecutionContext
from .policy import ExecutionPolicy
from .quality import QualityReport

#: Максимум значений одного поля аргумента в ``payload``. Список на 5000
#: элементов — это уже не аргумент, а утечка ответа обратно в журнал.
MAX_LOGGED_ITEMS = 20
MAX_LOGGED_ITEM_CHARS = 200

#: Длина хеша в журнале. Полный sha256 — это 64 символа, и в журнале событий они
#: занимают место, не добавляя смысла: для сверки «тот же результат или нет»
#: хватает 16 символов (64 бита), а колонка остаётся читаемой.
HASH_CHARS = 16

#: Маркер усечённой выдержки. Виден в payload, иначе усечённое тело нельзя
#: отличить от целого — а это ложь того же класса, что и отсутствие строки.
TRUNCATION_MARKER = "…[обрезано]"

#: Маркер замаскированного значения.
REDACTED = "[скрыто]"

#: Формы, по которым секрет узнаётся без знания имени поля. Список закрыт
#: осознанно: секрет под безобидным именем и внутри свободного текста не
#: ловится, и семантическая редактура здесь дала бы ложную уверенность.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM-блок: приватный ключ целиком, вместе с переводами строк.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # DSN: схема://пользователь:пароль@хост/база — пароль в URI.
    (
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|redis|mongodb(?:\+srv)?|amqp|https?)://\S+"),
        REDACTED,
    ),
    # ``Bearer <token>``: сохраняем слово-указатель, тело — нет.
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
    # Ключ провайдера открытого вида.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    # JWT-тройка.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
        REDACTED,
    ),
)


def _canonical(value: Any) -> str:
    """Канонический текст значения: ключи отсортированы, несериализуемое — как текст.

    Канонизация нужна для одного: два одинаковых по смыслу аргумента обязаны
    давать один хеш, иначе сравнение перестаёт значить ничего.
    """
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def measure(value: Any) -> tuple[int, str]:
    """Размер в байтах и усечённый sha256 значения — пара, которая идёт в журнал."""
    raw = _canonical(value).encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()[:HASH_CHARS]


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_LOGGED_ITEM_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _short(item) for key, item in list(value.items())[:MAX_LOGGED_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_short(item) for item in list(value)[:MAX_LOGGED_ITEMS]]
    return str(value)[:MAX_LOGGED_ITEM_CHARS]


def _mask(value: Any, keys: frozenset[str], counter: list[int]) -> Any:
    """Скрыть секреты по имени поля и по форме значения.

    Обе маскировки нужны, и обе — в одной функции: выдержка и структурное поле
    берутся из одного прохода, иначе маскирование пришлось бы повторять в двух
    местах, а через месяц они разошлись бы.

    Имя поля ловит то, что формой не узнать («пароль из хранилища»), форма —
    то, у чего имени нет (``Bearer …`` в свободном тексте). Счётчик — список,
    потому что обход рекурсивный, а возвращать пару «значение, счётчик» на
    каждом уровне значило бы размножить типы.
    """
    if isinstance(value, Mapping):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).strip().lower()
            if name in keys:
                counter[0] += 1
                masked[str(key)] = REDACTED
            else:
                masked[str(key)] = _mask(item, keys, counter)
        return masked
    if isinstance(value, (list, tuple)):
        return [_mask(item, keys, counter) for item in value]
    if isinstance(value, str):
        return _redact_by_form(value, counter)
    return value


def _redact_by_form(text: str, counter: list[int]) -> str:
    """Скрыть секреты, узнаваемые по форме, независимо от имени поля."""
    for pattern, replacement in _SECRET_PATTERNS:
        text, hits = pattern.subn(replacement, text)
        counter[0] += hits
    return text


def excerpt(
    value: Any, *, cap: int, redact_keys: tuple[str, ...] = ()
) -> tuple[str | None, bool, int]:
    """Ограниченная выдержка значения для ``payload``.

    Возвращает ``(текст, усечено, замаскировано)``.

    Порядок именно такой: сначала маскирование, потом сериализация, потом
    усечение. Обратный порядок унёс бы в журнал начало секрета — ровно то, что
    маскирование и обещало убрать.

    Значение **не** укорачивается по полям перед сериализацией: иначе потолок
    оказался бы фикцией (двадцать полей по двести символов никогда не дошли бы
    до него), а маркер усечения — ложью, которой нет там, где тело на самом деле
    обрезано заранее.

    Неположительный потолок означает «выдержку не пишем». Значение по умолчанию
    в коде было бы фикцией: потолок приходит политикой из ``platform.json``, и
    нулевой запасной путь тихо отключил бы то, что объявлено.
    """
    if cap <= 0:
        return None, False, 0
    counter = [0]
    keys = frozenset(key.strip().lower() for key in redact_keys)
    text = _canonical(_mask(value, keys, counter))
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text, False, counter[0]
    marker = TRUNCATION_MARKER.encode("utf-8")
    # Режем по границе символа: срез по байтам разорвал бы многобайтовый символ
    # и дал нечитаемый хвост выдержки.
    body = raw[: max(0, cap - len(marker))].decode("utf-8", errors="ignore")
    return f"{body}{TRUNCATION_MARKER}", True, counter[0]


def collect_argument_fields(
    arguments: Mapping[str, Any], policy: ExecutionPolicy
) -> dict[str, Any]:
    """Отобрать из аргументов те поля, что политика разрешает писать в журнал."""
    if not policy.log_argument_fields:
        return {}
    return {
        field: arguments[field]
        for field in policy.log_argument_fields
        if field in arguments
    }


def _argument_payload(
    arguments: Mapping[str, Any], policy: ExecutionPolicy
) -> dict[str, Any]:
    """Поля выдержки аргументов: структура по белому списку + ограниченный текст.

    Две формы не дублируют друг друга, а отвечают на разные вопросы: структура
    пригодна для чтения по полям, выдержка показывает тело там, где структура
    не помещается. Ни одна из них не снимает потолок с другой.

    Маскирование — **один проход на оба поля**. Два прохода разошлись бы: в
    структуре секрет остался бы открытым, пока в выдержке рядом стоял бы
    маркер, и требование закрывалось бы ровно наполовину.
    """
    size, digest = measure(arguments)
    payload: dict[str, Any] = {
        "arguments_size": size,
        "arguments_hash": digest,
    }
    selected = collect_argument_fields(arguments, policy)
    if not selected:
        return payload
    counter = [0]
    keys = frozenset(key.strip().lower() for key in policy.log_redact_keys)
    masked = _mask(selected, keys, counter)
    payload["arguments"] = {field: _short(value) for field, value in masked.items()}
    if policy.log_argument_excerpt_bytes > 0:
        # ``redact_keys`` пустой: значение уже замаскировано, повторный проход
        # считал бы одно и то же значение дважды.
        text, truncated, _masked = excerpt(
            masked, cap=policy.log_argument_excerpt_bytes, redact_keys=()
        )
        payload["arguments_excerpt"] = text
        payload["arguments_truncated"] = truncated
        payload["arguments_masked"] = counter[0]
    return payload



def _base_metadata(ctx: ToolExecutionContext, policy: ExecutionPolicy) -> dict[str, Any]:
    """Служебные признаки события.

    ``source`` и ``component`` едут в ``metadata``, а не в отдельные колонки:
    колонки в журнале появляются миграцией, а не правкой кода, и пока
    миграции нет, новый столбец означал бы потерю всех событий при откате.
    """
    return {
        "source": SOURCE_ENTERPRISE_MCP,
        "component": COMPONENT_TOOL_EXECUTION,
        "capability": ctx.capability,
        "execution_policy": {
            "max_inline_result_bytes": policy.max_inline_result_bytes,
            "execution_timeout_sec": policy.execution_timeout_sec,
            "persist_large_results": policy.persist_large_results,
            "quality_check_enabled": policy.quality_check_enabled,
        },
        **dict(ctx.metadata),
    }


class ExecutionLogger:
    """Сборка событий одного вызова.

    Писателя (``EventWriter``) получает снаружи: так тест проверяет форму
    событий, не поднимая буфер журнала, а рантайм — один и тот же писатель на
    конвейер и на всё остальное.
    """

    def __init__(self, writer: Any, *, now: Any = None) -> None:
        self._writer = writer
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def writer(self) -> Any:
        return self._writer

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def _emit(self, event: AgentEvent) -> str:
        if self._writer is None:
            return "dropped"
        return self._writer.emit(event)

    # -- шаги конвейера -----------------------------------------------------

    def started(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        arguments: Mapping[str, Any],
    ) -> str:
        size, digest = measure(arguments)
        payload: dict[str, Any] = {
            "tool_name": ctx.tool_name,
            "capability": ctx.capability,
            "started_at": ctx.started_at.isoformat(),
        }
        payload.update(_argument_payload(arguments, policy))
        return self._emit(
            AgentEvent(
                event_type=TOOL_STARTED,
                level="INFO",
                name=ctx.tool_name,
                summary=f"{ctx.tool_name}: начало",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload=payload,
                metadata=_base_metadata(ctx, policy),
                timestamp=self._now(),
            )
        )

    def completed(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        result: Any,
        *,
        duration_ms: int,
        quality: QualityReport | None = None,
        large_result: bool = False,
        artifact: Artifact | None = None,
    ) -> str:
        size, digest = measure(result)
        payload: dict[str, Any] = {
            "tool_name": ctx.tool_name,
            "capability": ctx.capability,
            "started_at": ctx.started_at.isoformat(),
            "finished_at": self._now().isoformat(),
            "duration_ms": duration_ms,
            "status": "ok",
            "result_size": size,
            "result_hash": digest,
        }
        text, truncated, masked = excerpt(
            result,
            cap=policy.log_result_excerpt_bytes,
            redact_keys=policy.log_redact_keys,
        )
        if text is not None:
            payload["result_excerpt"] = text
            payload["result_truncated"] = truncated
            payload["result_masked"] = masked
        metadata = _base_metadata(ctx, policy)
        metadata["large_result"] = bool(large_result)
        if artifact is not None:
            payload["artifact_id"] = artifact.artifact_id
            payload["artifact_uri"] = artifact.uri
        return self._emit(
            AgentEvent(
                event_type=TOOL_COMPLETED,
                level="INFO",
                name=ctx.tool_name,
                summary=f"{ctx.tool_name}: успех за {duration_ms} мс",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload=payload,
                metadata=metadata,
                timestamp=self._now(),
            )
        )

    def failed(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        *,
        error_code: str,
        message: str,
        duration_ms: int,
        arguments: Mapping[str, Any],
        timed_out: bool = False,
    ) -> str:
        size, digest = measure(arguments)
        payload: dict[str, Any] = {
            "tool_name": ctx.tool_name,
            "capability": ctx.capability,
            "started_at": ctx.started_at.isoformat(),
            "finished_at": self._now().isoformat(),
            "duration_ms": duration_ms,
            "status": "timeout" if timed_out else "error",
            "error_code": error_code,
            "error_message": message,
            "arguments_size": size,
            "arguments_hash": digest,
        }
        # Выдержка аргументов у отказа обязательна особенно: когда операция
        # отказала, именно аргументы отвечают на вопрос «с чем её звали».
        payload.update(_argument_payload(arguments, policy))
        return self._emit(
            AgentEvent(
                event_type=TOOL_TIMEOUT if timed_out else TOOL_FAILED,
                level="WARN" if timed_out else "ERROR",
                name=ctx.tool_name,
                summary=f"{ctx.tool_name}: отказ {error_code} за {duration_ms} мс",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload=payload,
                metadata=_base_metadata(ctx, policy),
                timestamp=self._now(),
            )
        )

    def artifact_created(
        self, ctx: ToolExecutionContext, policy: ExecutionPolicy, artifact: Artifact
    ) -> str:
        return self._emit(
            AgentEvent(
                event_type=ARTIFACT_CREATED,
                level="INFO",
                name=ctx.tool_name,
                summary=f"результат сохранён артефактом ({artifact.size} байт)",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload={
                    "tool_name": ctx.tool_name,
                    "artifact_id": artifact.artifact_id,
                    "artifact_name": artifact.name,
                    "artifact_size": artifact.size,
                    "artifact_content_type": artifact.content_type,
                    "artifact_uri": artifact.uri,
                },
                metadata={
                    **_base_metadata(ctx, policy),
                    "reason": "large_result",
                },
                timestamp=self._now(),
            )
        )

    def quality_checked(
        self, ctx: ToolExecutionContext, policy: ExecutionPolicy, report: QualityReport
    ) -> str:
        # Уровень отражает тяжесть замечаний, а не сам факт проверки.
        #
        # Технический провал сюда не доходит: конвейер возвращает отказ раньше
        # (``technical_failure``), поэтому любой оставшийся флаг — семантический,
        # то есть «результат пригоден, но подозрителен». Это и есть определение
        # WARN в требовании «Уровни логирования несут смысл»: оборот
        # продолжается, но деградировал.
        #
        # Без замечаний уровень — диагностический. Замер: 46 из 48 строк
        # ``quality.check`` несли ноль информации, и это 13 % таблицы; писать
        # их как INFO было нечем. Теперь такие события не доходят до журнала
        # при пороге по умолчанию, а отбрасывание видно в ``suppressed_noise``.
        flags = report.flags
        return self._emit(
            AgentEvent(
                event_type=QUALITY_CHECK,
                level="WARN" if flags else DIAGNOSTIC_LEVEL,
                name=ctx.tool_name,
                summary=f"качество: {report.policy} ({', '.join(flags) or 'без замечаний'})",
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                request_id=ctx.request_id,
                payload={"tool_name": ctx.tool_name, **report.to_json()},
                metadata=_base_metadata(ctx, policy),
                timestamp=self._now(),
            )
        )
