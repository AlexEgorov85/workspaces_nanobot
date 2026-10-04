"""Конвейер исполнения операции.

Девять шагов, один вход, один выход. Зачем он нужен при работающем сервере:
до него каждая из шестнадцати операций решала за себя, где взять идентичность,
писать ли в журнал, что делать с большим ответом и во что превращать
исключение. Решения расходились, и расхождение было видно только по журналу.

Шаги:

1. **идентичность** — из ``params._meta``; ни при каких условиях не достраивается;
2. **политика** — «платформа → capability → операция»;
3. **контекст** — снимок вызова, который дальше только читается;
4. **начало** — ``tool.started``, аргументы меряются и хешируются;
5. **вызов домена** — с пределом времени;
6. **качество** — по политике операции;
7. **размер** — крупный результат сохраняется артефактом **до** возврата;
8. **ответ** — доменное тело сохраняется, метаданные добавляются рядом;
9. **итог** — ``tool.completed``/``tool.failed``/``tool.timeout``.

Ограничение шага 5, о котором стоит знать: предел времени реализован как
«перестать ждать», а не «прервать работу». Поток Python нельзя убить, поэтому
операция, не уложившаяся в срок, продолжает работать в фоне, а её результат
отбрасывается: он не попадает ни в ответ, ни в артефакт, ни в журнал успеха.
Убийство процесса вместо этого — приём, который унёс бы за собой буфер журнала
и вместе с ним событие о том, что процесс упал.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..errors import InfrastructureError
from ..eventing.models import (
    COMPONENT_TOOL_EXECUTION,
    SOURCE_ENTERPRISE_MCP,
    AgentEvent,
)
from ..eventing.types import TOOL_FAILED
from ..session.artifact_store import Artifact, ArtifactStore
from ..session.workspace import SessionWorkspace
from .context import (
    CONTEXT_PARAM,
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    KEY_USER_ID,
    IdentityMissingError,
    McpCallContext,
    ToolExecutionContext,
)
from .errors import ExecutionTimeout, Failure, failure_from_payload, normalize_exception
from .logger import ExecutionLogger
from .policy import ExecutionPolicy, resolve_policy
from .quality import QualityChecker, QualityReport, technical_failure

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from ..registry import ToolDefinition

log = logging.getLogger(__name__)

#: Ключ, под которым в ответе лежат метаданные исполнения. Зарезервирован:
#: операция вправе вернуть такой ключ в своих данных, и тогда тело уходит в
#: конверт, а не перезаписывается.
EXECUTION_KEY = "_execution"

#: Ключи, из которых идентичность берётся в переходном окне
#: (``execution.require_call_meta = false``). Именно в них вызывающая сторона
#: передавала идентичность до перехода на ``_meta``.
LEGACY_IDENTITY_KEYS: tuple[str, str, str] = ("request_id", "session_id", "user_id")

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Итог одного вызова операции.

    ``text`` — то, что уходит в MCP-ответ. ``is_error`` отличает отказ от
    результата; в MCP оба случая возвращаются успешно, различается лишь флаг
    ``isError`` в протоколе, и путать их нельзя.
    """

    text: str
    is_error: bool
    code: str
    context: ToolExecutionContext | None
    policy: ExecutionPolicy | None
    status: str
    duration_ms: int = 0
    quality: QualityReport | None = None
    artifact: Artifact | None = None
    raw: Any = None
    notes: tuple[str, ...] = field(default_factory=tuple)


class ToolExecutionPipeline:
    """Единая обвязка вызова операции."""

    def __init__(
        self,
        *,
        base_policy: ExecutionPolicy,
        policy_overrides: Mapping[str, Any] | None = None,
        workspace: SessionWorkspace | None = None,
        artifact_store: ArtifactStore | None = None,
        writer: Any = None,
        quality: QualityChecker | None = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._base = base_policy
        self._overrides = dict(policy_overrides or {})
        self._workspace = workspace
        self._artifacts = artifact_store
        self._quality = quality or QualityChecker()
        self._logger = ExecutionLogger(writer if base_policy.logging_enabled else None)
        # Пул потоков конвейера — отдельный от пула БД: там считают соединения,
        # здесь — операции в полёте. Размер задаёт composition root, потому что
        # число одновременных вызовов — свойство развёртывания, а не модуля.
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tool-exec"
        )

    @property
    def writer(self) -> Any:
        return self._logger.writer

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    # -- вызов --------------------------------------------------------------

    def execute(
        self,
        definition: ToolDefinition,
        arguments: Mapping[str, Any] | None,
        meta: Mapping[str, Any] | None,
    ) -> PipelineResult:
        started_at = datetime.now(UTC)
        call_args = dict(arguments or {})
        tool_name = definition.name
        capability = definition.category

        # Шаги 1–3. Идентичность разбирается до всего остального: без неё
        # вызов не начинается, и ни журнал, ни каталог сессии не создаются.
        try:
            call, call_args, identity_source = self._resolve_identity(
                meta, call_args, tool_name=tool_name, policy=self._base
            )
        except IdentityMissingError as exc:
            failure = normalize_exception(exc)
            self._report_identity_rejection(tool_name, capability, failure)
            return PipelineResult(
                text=json.dumps(failure.to_json(), ensure_ascii=False),
                is_error=True,
                code=failure.code,
                context=None,
                policy=self._base,
                status=STATUS_ERROR,
                notes=(f"операция {tool_name}",),
            )

        policy = resolve_policy(
            self._base,
            capability=capability,
            tool=tool_name,
            overrides=self._overrides,
        )
        ctx = ToolExecutionContext(
            call=call,
            tool_name=tool_name,
            capability=capability,
            started_at=started_at,
            metadata={"identity_source": identity_source},
        )

        # Шаг 4.
        self._logger.started(ctx, policy, call_args)

        # Шаг 5.
        try:
            raw = self._call_domain(definition, ctx, call_args, policy)
        except Exception as exc:  # noqa: BLE001 - наружу уходит нормализованный отказ
            return self._refuse(ctx, policy, normalize_exception(exc), call_args)

        # Отказ, объявленный телом ответа, а не исключением: часть операций
        # сигналит так о штатных исходах («задачи нет»). Пропустить его — значит
        # отдать вызывающему отказ как успех.
        declared = failure_from_payload(raw)
        if declared is not None:
            return self._refuse(ctx, policy, declared, call_args)

        parsed = _as_json_value(raw)
        duration_ms = ctx.duration_ms

        # Шаг 6.
        report: QualityReport | None = None
        if policy.quality_check_enabled:
            report = self._quality.check(definition.quality_policy, parsed)
            broken = technical_failure(report)
            if broken is not None:
                return self._refuse(ctx, policy, broken, call_args)
            self._logger.quality_checked(ctx, policy, report)

        # Шаг 7.
        size = _measure_response(raw)
        artifact: Artifact | None = None
        notes: list[str] = []
        # Порог 0 — это «порог не задан», а не «сохранять всё». Иначе сервер,
        # у которого ключ не объявлен, начал бы складывать в артефакты каждый
        # ответ, включая однострочный, и платил бы за это двумя лишними
        # файловыми операциями на вызов.
        large_result = policy.max_inline_result_bytes > 0 and size > policy.max_inline_result_bytes
        # Тело ответа собирается из ``parsed``, а не из ``raw``: операции
        # возвращают JSON-строкой, и взятый из ``raw`` текст не является
        # объектом — тогда ``_compose_response`` оставил бы его байт-в-байт и
        # метаданные вызова не дошли бы до вызывающего ни разу. Для обычного
        # текста ``_as_json_value`` возвращает ``raw`` без изменений, так что
        # правило «строку не оборачивать» продолжает действовать.
        body: Any = parsed
        if large_result:
            try:
                artifact, body, notes = self._persist_large(
                    ctx, policy, tool_name, raw, parsed, size
                )
            except Exception as exc:  # noqa: BLE001 - отказ записи, а не домена
                return self._refuse(ctx, policy, normalize_exception(exc), call_args)

        # Шаг 8.
        execution_meta: dict[str, Any] = {
            "request_id": ctx.request_id,
            "tool": tool_name,
            "capability": capability,
            "duration_ms": duration_ms,
            "result_size": size,
        }
        if report is not None:
            execution_meta["quality"] = report.to_json()
        if large_result and artifact is None:
            # Порог превышен, но результат не сохранён: сообщить об этом в ответе
            # обязательно, иначе вызывающий решит, что тело урезали молча.
            execution_meta["large_result"] = True
        if artifact is not None:
            execution_meta["artifact"] = artifact.to_json()
        text = _compose_response(body, execution_meta)

        # Шаг 9.
        self._logger.completed(
            ctx,
            policy,
            raw,
            duration_ms=duration_ms,
            quality=report,
            large_result=artifact is not None,
            artifact=artifact,
        )
        return PipelineResult(
            text=text,
            is_error=False,
            code="",
            context=ctx,
            policy=policy,
            status=STATUS_OK,
            duration_ms=duration_ms,
            quality=report,
            artifact=artifact,
            raw=raw,
            notes=tuple(notes),
        )

    # -- шаги ---------------------------------------------------------------

    def _resolve_identity(
        self,
        meta: Mapping[str, Any] | None,
        arguments: dict[str, Any],
        *,
        tool_name: str,
        policy: ExecutionPolicy,
    ) -> tuple[McpCallContext, dict[str, Any], str]:
        """Идентичность вызова и очищенные доменные аргументы.

        Ключи идентичности вырезаются из аргументов **всегда**, независимо от
        режима. При полном ``_meta`` они игнорируются как источник, но если
        оставить их в аргументах, значение, присланное моделью под именем
        ``session_id``, дойдёт до операции — а в опубликованной схеме такого
        поля нет, то есть это будет поле, которого не существует.

        Строгий режим (``require_call_meta = true``) берёт идентичность только
        из ``_meta``. Переходное окно допускает чтение из ``arguments`` — и
        только когда ``_meta`` отсутствует: смешивать два источника нельзя,
        иначе событие в журнале и файл сессии описывали бы разные вызовы.
        """
        cleaned = {
            key: value for key, value in arguments.items() if key not in LEGACY_IDENTITY_KEYS
        }
        try:
            return McpCallContext.from_meta(meta), cleaned, "meta"
        except IdentityMissingError:
            if not policy.require_call_meta:
                legacy = {
                    key: arguments.get(key)
                    for key in LEGACY_IDENTITY_KEYS
                    if arguments.get(key)
                }
                if legacy:
                    log.warning(
                        "%s: вызов без params._meta, идентичность взята из arguments "
                        "переходным путём (require_call_meta=false)",
                        tool_name,
                    )
                    return McpCallContext.from_meta(_as_meta(legacy)), cleaned, "arguments"
            raise

    def _call_domain(
        self,
        definition: ToolDefinition,
        ctx: ToolExecutionContext,
        arguments: Mapping[str, Any],
        policy: ExecutionPolicy,
    ) -> Any:
        """Вызвать обработчик с пределом времени.

        Поток, не уложившийся в срок, не прерывается — Python не умеет этого.
        Его результат отбрасывается целиком: ни в ответ, ни в артефакт, ни в
        ``tool.completed`` он не попадает.
        """
        kwargs: dict[str, Any] = dict(arguments)
        if definition.wants_context():
            kwargs[CONTEXT_PARAM] = ctx
        timeout = policy.execution_timeout_sec
        if timeout <= 0:
            return definition.handler(**kwargs)

        future: Future[Any] = self._executor.submit(definition.handler, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise ExecutionTimeout(
                f"операция не уложилась в {timeout:g} с"
            ) from exc

    def _persist_large(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        tool_name: str,
        raw: Any,
        parsed: Any,
        size: int,
    ) -> tuple[Artifact | None, Any, list[str]]:
        """Сохранить крупный результат артефактом.

        Порядок именно такой: сначала запись, потом ответ. Ответ со ссылкой на
        несуществующий файл хуже, чем явный отказ — по ссылке агент пошёл бы
        читать и не нашёл бы ничего.
        """
        if not policy.persist_large_results:
            return None, raw, ["large_result"]
        if self._artifacts is None:
            return None, raw, ["large_result_no_store"]
        content = raw if isinstance(raw, (str, bytes)) else json.dumps(
            parsed, ensure_ascii=False, indent=2
        ).encode("utf-8")
        raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
        try:
            artifact = self._artifacts.create(
                ctx.session_id,
                name="result.json",
                content=raw_bytes,
                content_type="application/json",
                tool_name=tool_name,
                request_id=ctx.request_id,
                subdir="results",
                folder=ctx.request_id,
            )
        except Exception as exc:  # noqa: BLE001 - наружу уходит нормализованный отказ
            # Отдельный случай, а не «стать как все»: ответ со ссылкой на
            # несуществующий файл хуже явного отказа — по ссылке вызывающий
            # пошёл бы читать и не нашёл бы ничего.
            raise InfrastructureError(f"крупный результат не сохранён: {exc}") from exc
        self._logger.artifact_created(ctx, policy, artifact)
        preview = raw_bytes[: policy.preview_bytes].decode("utf-8", errors="replace")
        body = {
            "result_saved": True,
            "large_result": True,
            "artifact_id": artifact.artifact_id,
            "size": artifact.size,
            "content_type": artifact.content_type,
            "uri": artifact.uri,
            "preview": preview,
        }
        return artifact, body, []

    def _report_identity_rejection(
        self,
        tool_name: str,
        capability: str,
        failure: Failure,
    ) -> None:
        """Сделать отказ по идентичности видимым.

        Отказ без следа — худший исход на границе безопасности: вызывающий
        перестаёт присылать ``params._meta``, операции отдают чистый отказ,
        в журнале не появляется ни строки, и причина находится только в коде
        вызывающей стороны. Поэтому след оставляется здесь, а не «в журнале
        шага 9»: до ``_logger.started`` дело не доходит, а сам журнал ведётся
        по контексту вызова, который без ``_meta`` построить нельзя.

        Личность при этом **не достраивается**: колонки ``session_id``,
        ``user_id`` и ``request_id`` допускают ``NULL``, и пустая личность в
        строке честнее выдуманной — иначе след в журнале указывал бы на
        чужую сессию. Писателя может не быть (сервер без capability ``data``),
        тогда остаётся строка в stderr: отказ на границе безопасности не
        должен выглядеть как тишина.
        """
        log.warning(
            "%s (%s): вызов отклонён — %s: %s. Идентичность не пришла в "
            "params._meta, поэтому шаг «начало» не выполнился и событие "
            "пишется в журнал без идентичности (достраивать её нельзя)",
            tool_name,
            capability,
            failure.code,
            failure.message,
        )
        writer = self._logger.writer
        if writer is None:
            return
        writer.emit(
            AgentEvent(
                event_type=TOOL_FAILED,
                level="error",
                name=tool_name,
                summary=(
                    f"{tool_name}: отказ на границе идентичности "
                    f"({failure.code}): {failure.message}"
                ),
                payload={
                    "error_code": failure.code,
                    "message": failure.message,
                    "capability": capability,
                    "identity_missing": True,
                    "identity_source": None,
                },
                # Признак писателя обязателен и на этом пути: отказ на границе
                # идентичности тоже пишет сторона, которая его видит, а без
                # `source` строку нечем отличить от отказа, записанного агентом
                # (тот пишет `nanobot`). Различие по `metadata.source` — вместо
                # нового имени события, которое при `strict` пришлось бы ещё и
                # объявлять в словаре платформы.
                metadata={
                    "source": SOURCE_ENTERPRISE_MCP,
                    "component": COMPONENT_TOOL_EXECUTION,
                    "logged_without_identity": True,
                },
            )
        )

    def _refuse(
        self,
        ctx: ToolExecutionContext,
        policy: ExecutionPolicy,
        failure: Failure,
        arguments: Mapping[str, Any],
    ) -> PipelineResult:
        duration_ms = ctx.duration_ms
        self._logger.failed(
            ctx,
            policy,
            error_code=failure.code,
            message=failure.message,
            duration_ms=duration_ms,
            arguments=arguments,
            timed_out=failure.code == "timeout",
        )
        return PipelineResult(
            text=json.dumps(failure.to_json(), ensure_ascii=False),
            is_error=True,
            code=failure.code,
            context=ctx,
            policy=policy,
            status=STATUS_TIMEOUT if failure.code == "timeout" else STATUS_ERROR,
            duration_ms=duration_ms,
        )


# -- вспомогательное ---------------------------------------------------------


def _as_meta(identity: Mapping[str, Any]) -> dict[str, str]:
    """Перевести плоские ключи идентичности в форму ``params._meta``.

    Неполный набор — тоже набор: отсутствующие ключи просто не попадают в
    словарь, и решение «хватает ли идентичности» принимает
    :meth:`McpCallContext.from_meta`, который перечисляет недостающие ключи в
    отказе. Обращение к отсутствующему ключу здесь дало бы ``KeyError`` вместо
    ``identity_missing``, и вызывающий получил бы внутреннюю ошибку платформы
    вместо ответа о контракте.
    """
    meta = {
        KEY_REQUEST_ID: identity.get("request_id"),
        KEY_SESSION_ID: identity.get("session_id"),
        KEY_USER_ID: identity.get("user_id"),
    }
    return {key: str(value) for key, value in meta.items() if value}


def _as_json_value(raw: Any) -> Any:
    """Разобрать тело ответа для проверок качества.

    Операции возвращают JSON-строку (так пишет их `_as_text` в capability), но
    могут вернуть и готовый объект. Разбор здесь, а не в проверках: проверка
    должна видеть форму результата, а не детали его упаковки.
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text or text[0] not in "[{\"":
        return raw
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw


def _measure_response(raw: Any) -> int:
    """Размер ответа в байтах.

    Меряется то, что реально уедет в MCP: для JSON-строки это её собственные
    байты, иначе метрика «крупный результат» считала бы размером текста,
    которого в ответе не будет.
    """
    if isinstance(raw, str):
        return len(raw.encode("utf-8"))
    try:
        return len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(raw).encode("utf-8"))


def _compose_response(body: Any, execution_meta: Mapping[str, Any]) -> str:
    """Тело ответа операции и метаданные исполнения рядом с ним.

    Правило одно: тело сохраняется как есть. Если это объект без своего
    ``_execution`` — метаданные добавляются ключом в тот же объект. Иначе тело
    уходит в конверт ``{"result": ..., "_execution": {...}}``.
    """
    if isinstance(body, dict) and EXECUTION_KEY not in body:
        merged = dict(body)
        merged[EXECUTION_KEY] = dict(execution_meta)
        return json.dumps(merged, ensure_ascii=False)
    if isinstance(body, (dict, list)):
        return json.dumps(
            {"result": body, EXECUTION_KEY: dict(execution_meta)}, ensure_ascii=False
        )
    # Не-объектное тело: конверт обернул бы строку, и вызывающий, который
    # сегодня читает `hits` из текста, получил бы вместо ответа описание ответа.
    return body if isinstance(body, str) else json.dumps(
        {"result": body, EXECUTION_KEY: dict(execution_meta)}, ensure_ascii=False
    )
