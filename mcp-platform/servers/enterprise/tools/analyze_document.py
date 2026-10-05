"""Операция ``analyze_document`` — разбор юридического документа.

Замысел. Разбор жил в capability, у которой нет доступа к сессии: фабрика
получает только контейнер, а сигнатура не содержит ``ctx``, то есть
``session_id`` был недоступен никак. Файлы состояния лежат в папке сессии, а она
приходит из контекста вызова — поэтому операция платформенная, рядом с
``read_result`` и ``session_files``, а не внутри ``capabilities/legal_summarizer/``.

Почему шаг «файл → текст» здесь, а не в домене. ``run()`` принимает ``text``
первым позиционным аргументом, а путь идёт отдельным ``document_path`` и в
содержимое не попадает. Загрузчик домена бросает голый ``ValueError``, и разбор
причины по тексту исключения хрупок: смена формулировки в домене тихо меняет
код отказа. Здесь пять исходов различаются **до** и **после** вызова загрузчика,
по существу причины, а не по её формулировке.

Почему путь проверяет ``safe_child``. Абсолютный путь, буква диска, ``..`` и
``..``/``.`` как единственный сегмент отвергает примитив платформы. Свой разбор
строки был бы вторым правилом на той же границе и разошёлся бы с ним при первой
же правке.

Почему режим загрузки обязателен. ``brief`` у PDF режет документ до 100 страниц
и 300 000 символов, то есть меняет ``chunks_total``, оценку, порог подтверждения
и цену. Молчаливый выбор одного из двух означал бы, что модель платит за полный
разбор, думая, что запросила краткий.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from libs.enterprise_common.errors import EnterpriseError, InvalidRequestError
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import (
    ToolDefinition,
    build_input_schema,
    validate_handler,
)
from libs.enterprise_common.session.security import PathDeniedError, safe_child
from libs.enterprise_common.session.workspace import SessionHandle, SessionWorkspace

logger = logging.getLogger(__name__)

#: Подкаталог состояния операции внутри ``artifacts/`` сессии.
#:
#: Имя повторяет имя capability, но это не совпадение: ``cache.manifest``
#: добавляет ``operations/<operation_id>`` сам, поэтому ``manifest_root``
#: получает каталог **до** сегмента ``operations`` — то есть
#: ``<каталог_сессии>/artifacts/legal_summarizer``. Передать подкаталог
#: ``artifacts`` нельзя (получится ``artifacts/operations/<id>`` без уровня
#: ``legal_summarizer/``), передать уже финальный путь тоже нельзя (получится
#: ``…/operations/<id>/operations/<id>``).
SKILL_DIRNAME = "legal_summarizer"

#: Подкаталог сессии, где лежит состояние операции. Задаётся **явно** в каждом
#: вызове ручки: молчаливый дефолт у ``write_text``/``read_text``/``exists`` —
#: ``responses``, а у ``write_bytes``/``read_bytes``/``list_files``/``remove`` —
#: ``results``, то есть буквальная реализация положила бы состояние не туда.
ARTIFACTS_SUBDIR = "artifacts"

#: Подкаталог вложений агента внутри сессии.
FILES_SUBDIR = "files"

#: Префикс ссылок сессии.
URI_SCHEME = "session://"

#: Срок жизни состояния. Объявлен здесь, а не выведен из замера: замеров
#: давности состояния в репозитории нет.
INCOMPLETE_TTL_SEC = 24 * 60 * 60
COMPLETE_TTL_SEC = 7 * 24 * 60 * 60

#: Множитель потолка вызова в сроке жизни признака занятости. Два, а не один:
#: вызов, оборванный потолком, снимает признак в ``finally`` не всегда, и
#: следующий вызов обязан иметь право его перехватить.
BUSY_TTL_FACTOR = 2

#: Доля потолка вызова, отдаваемая батчам. Остальное — загрузка документа,
#: сбор контекста исполнения, reduce-фаза и запись состояния: всё это тоже
#: идёт внутри потолка, поэтому отдавать его батчам нельзя.
BATCH_BUDGET_SHARE = 0.6

#: Стоимость одного батча, если домен не отдал свою оценку. Объявлено в домене
#: (``llm/config.py`` → ``estimated_chunk_duration_sec``), поэтому значение
#: подставляется только при негодной конфигурации.
FALLBACK_CHUNK_SEC = 20.0

#: Имена событий разбора. Объявлены в ``eventing/types.py`` другим
#: исполнителем — здесь они только используются, повторное объявление было бы
#: вторым словарём имён.
EVENT_STEP = "legal_analysis_step"
EVENT_CONFIRMATION = "legal_analysis_confirmation"
EVENT_COMPLETED = "legal_analysis_completed"
EVENT_PARTIAL = "legal_analysis_partial"
EVENT_REFUSED = "legal_analysis_refused"


# ── корень состояния ──────────────────────────────────────────────────────
#
# Одна функция на обе операции. Писатель и читатель обязаны приходить к одному
# корню: пока читатель разрешал корень сам (из ``cache_root``), любой
# ``query_operation`` получал ``manifest_not_found`` на только что созданном
# состоянии.


def state_root(handle: SessionHandle) -> Path:
    """Каталог состояния операций этой сессии — до сегмента ``operations``."""
    return handle.subdir(ARTIFACTS_SUBDIR) / SKILL_DIRNAME


def operations_dir(handle: SessionHandle) -> Path:
    """Каталог состояний операций — то, что примет ``operations/``."""
    return state_root(handle) / "operations"


def busy_marker(operation_id: str) -> str:
    """Относительный путь признака занятости внутри ``artifacts/``."""
    return f"{SKILL_DIRNAME}/busy/{operation_id}.json"


def access_marker(operation_id: str) -> str:
    """Относительный путь отметки последнего обращения внутри ``artifacts/``."""
    return f"{SKILL_DIRNAME}/access/{operation_id}.json"


# ── разбор аргументов ─────────────────────────────────────────────────────


def _resolve_document(handle: SessionHandle, document: str) -> Path:
    """Путь файла документа внутри ``files/`` этой сессии.

    Принимаются две формы: ``session://files/<путь>`` и относительный путь
    внутри ``files/``. Перечисление форм — не запрещённая дизъюнкция: модель
    получает ссылку от конвейера и вправе назвать путь сама.
    """
    raw = (document or "").strip()
    if not raw:
        raise InvalidRequestError(
            "документ не задан: нужна session://-ссылка или путь внутри files/",
            code="invalid_params",
        )
    body = raw[len(URI_SCHEME) :] if raw.startswith(URI_SCHEME) else raw
    prefix = f"{FILES_SUBDIR}/"
    if body.startswith(prefix):
        body = body[len(prefix) :]
    elif body == FILES_SUBDIR:
        raise InvalidRequestError(
            f"ссылка {raw!r} указывает на каталог, а не на файл документа"
        )
    try:
        return safe_child(handle.subdir(FILES_SUBDIR), body)
    except PathDeniedError as exc:
        raise InvalidRequestError(str(exc), code="invalid_params") from exc


def _require_enum(value: str, allowed: tuple[str, ...], name: str) -> str:
    """Значение не из перечня — отказ с перечислением, а не молчаливая нормализация."""
    cleaned = (value or "").strip()
    if cleaned not in allowed:
        raise InvalidRequestError(
            f"{name}={value!r} недопустим; доступны: {', '.join(allowed)}",
            code="invalid_params",
        )
    return cleaned


def _load_document_text(path: Path, load_mode: str) -> str:
    """Текст документа; пять различаемых исходов отказа.

    ``document_not_found`` / ``not_a_file`` / ``unsupported_format`` отвергаются
    **до** вызова загрузчика — по признаку, а не по его исключению.
    ``document_unreadable`` — то, что загрузчик не смог прочитать, причём файл
    непустой. ``EMPTY_DOCUMENT`` — файл прочитан, а текста в нём нет: пустой
    файл читается успешно и пуст, то есть он **не** «нечитаем».
    """
    if not path.exists():
        raise EnterpriseError(
            f"документ {path.name!r} не найден в files/ сессии", code="document_not_found"
        )
    if not path.is_file():
        raise EnterpriseError(
            f"{path.name!r} — не файл, а каталог или ссылка", code="not_a_file"
        )
    if path.suffix.lower() not in {".pdf", ".docx", ".txt"}:
        raise EnterpriseError(
            f"формат {path.suffix or '(без расширения)'!r} не поддерживается: "
            "принимаются .pdf, .docx, .txt",
            code="unsupported_format",
        )

    from libs.legal_summarizer.application.document_io import load_text

    try:
        text = load_text(path, mode=load_mode)
    except PathDeniedError:
        raise
    except ValueError:
        # Причину разбираем по существу: пустой файл домен отдаёт как
        # ``EMPTY_DOCUMENT``, а нечитаемый — как ``document_unreadable``.
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise EnterpriseError(
                f"документ {path.name!r} не прочитан: {exc.__class__.__name__}",
                code="document_unreadable",
            ) from exc
        if not raw.strip():
            raise EnterpriseError(
                "документ не содержит текста", code="EMPTY_DOCUMENT"
            ) from None
        raise EnterpriseError(
            f"документ {path.name!r} не дал извлекаемого текста",
            code="document_unreadable",
        ) from None
    except OSError as exc:
        raise EnterpriseError(
            f"документ {path.name!r} не прочитан: {exc.__class__.__name__}",
            code="document_unreadable",
        ) from exc

    if not text or not text.strip():
        raise EnterpriseError("документ не содержит текста", code="EMPTY_DOCUMENT")
    return text


def _accepted_kwargs(func: Any, **kwargs: Any) -> dict[str, Any]:
    """Только те именованные аргументы, которых у функции уже есть.

    Домен получает ограничение шага (``batch_limit``) и ``focus`` в идентичность
    параллельной правкой. Операция обращается к ним по именам, а не по
    подписи конкретной версии: пока параметра нет, вызов не падает на нём.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - встроенные функции
        return {}
    return {name: value for name, value in kwargs.items() if name in parameters}


def _batch_budget(execution_timeout_sec: float) -> int:
    """Сколько батчей укладывается в потолок вызова.

    Потолок читает **вызывающая** сторона, а не домен: домен о потолке не знает
    и по условию задачи не должен (``service.py`` — «отказать по неделимости
    дело вызывающей стороны, которая знает потолок вызова»). Ограничение
    обязано быть и на ``map_reduce``, иначе разбор, не уложившийся в потолок,
    нечем продолжить: домен выполняет все батчи подряд, поток, не уложившийся
    в срок, не прерывается, а его возврат отбрасывается целиком.

    Ноль не возвращается: он означал бы «шаг не поместился», и домен отличал
    бы его от «шаг не начат» — это один и тот же отказ с двумя разными
    причинами. Единица означает, что вызов сделает один батч и вернёт
    ``requires_continuation`` с остатком.
    """
    try:
        from libs.legal_summarizer.llm.config import get_execution_config

        per_batch = float(get_execution_config().get("estimated_chunk_duration_sec") or FALLBACK_CHUNK_SEC)
    except (ImportError, AttributeError, TypeError, ValueError):
        per_batch = FALLBACK_CHUNK_SEC
    if per_batch <= 0:
        per_batch = FALLBACK_CHUNK_SEC
    usable = max(1.0, float(execution_timeout_sec) * BATCH_BUDGET_SHARE)
    return max(1, int(usable // per_batch))


# ── идентичность ──────────────────────────────────────────────────────────


def _compute_operation_id(text: str, length: str, *, document_path: str, question: str, focus: str) -> str:
    """Идентификатор операции из документа и параметров.

    Переданный вызывающей стороной ``operation_id`` сверяется с вычисленным:
    в пределах сессии подстановка чужого значения адресовала бы чужое
    состояние, и это не «не тот аргумент», а обход границы.
    """
    from libs.legal_summarizer.application.operation_id import make_operation_id

    computed = make_operation_id(
        text,
        length,
        **_accepted_kwargs(
            make_operation_id, document_path=document_path, question=question, focus=focus
        ),
    )
    return computed


def _verify_operation_id(given: str, computed: str) -> None:
    """Отказ по несовпадению — до открытия состояния и без единого LLM-вызова."""
    given = (given or "").strip()
    if not given:
        return
    if given != computed:
        raise InvalidRequestError(
            "operation_id не соответствует документу и параметрам вызова: "
            f"передан {given!r}, вычислен {computed!r}. "
            "Подстановка чужого идентификатора отвергается.",
            code="invalid_params",
        )


# ── признак занятости ────────────────────────────────────────────────────


def _read_marker(handle: SessionHandle, relative: str) -> dict[str, Any] | None:
    if not handle.exists(relative, subdir=ARTIFACTS_SUBDIR):
        return None
    try:
        raw = handle.read_text(relative, subdir=ARTIFACTS_SUBDIR)
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        # Битый признак занятости — не «занято» и не «свободно»: считаем его
        # истёкшим, иначе вызов блокировался бы навсегда.
        return None
    return data if isinstance(data, dict) else None


def _write_marker(handle: SessionHandle, relative: str, payload: dict[str, Any]) -> None:
    """Запись состояния операции; сорванная запись — ``state_write_failed``."""
    try:
        handle.write_json(relative, payload, subdir=ARTIFACTS_SUBDIR)
    except OSError as exc:
        raise EnterpriseError(
            f"состояние операции не записано: {exc.__class__.__name__}",
            code="state_write_failed",
        ) from exc


def _take_or_refuse(handle: SessionHandle, operation_id: str, *, ttl_sec: float, owner: str) -> int:
    """Решение о работе принимается **до** платной работы.

    Новый вызов читает ``step`` и ``session_token``; срок жизни признака не
    истёк — отказ ``operation_in_progress``, не сделав ни одного LLM-вызова.
    Истёк — работа берётся, счётчик продолжает расти, и оплаченное не
    оплачивается повторно.
    """
    now = time.time()
    existing = _read_marker(handle, busy_marker(operation_id))
    step = 0
    if existing is not None:
        try:
            expires_at = float(existing.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at > now:
            raise EnterpriseError(
                "операция уже выполняется в этом состоянии "
                f"(operation_id={operation_id}, session_token="
                f"{existing.get('session_token')!r}); дождитесь её ответа",
                code="operation_in_progress",
            )
        try:
            step = int(existing.get("step", 0))
        except (TypeError, ValueError):
            step = 0
    step += 1
    _write_marker(
        handle,
        busy_marker(operation_id),
        {
            "operation_id": operation_id,
            "step": step,
            "session_token": owner,
            "taken_at": now,
            "expires_at": now + ttl_sec,
        },
    )
    return step


def _release(handle: SessionHandle, operation_id: str) -> None:
    """Признак снимается в ``finally`` вызова."""
    try:
        handle.remove(busy_marker(operation_id), subdir=ARTIFACTS_SUBDIR)
    except OSError:  # pragma: no cover - снятие признака не обязано удаться
        logger.warning("признак занятости %s не снят", operation_id)


# ── обращения и уборка ────────────────────────────────────────────────────


def _touch(handle: SessionHandle, operation_id: str, *, status: str | None) -> None:
    """Отметка последнего обращения — по ней уборка знает возраст состояния."""
    _write_marker(
        handle,
        access_marker(operation_id),
        {
            "operation_id": operation_id,
            "last_access_at": time.time(),
            "status": status,
        },
    )


def _is_orphaned(operation_id: str, manifest: dict[str, Any], handle: SessionHandle) -> bool:
    """Предикат осиротевшего состояния: идентификатор больше не вычисляется.

    Пересчёт идёт из того же файла по тому же пути с теми же ``length`` и
    ``question``. Перебор всех допустимых вызовов неприменим — вызовов
    бесконечно много, и уборка не может их перечислить.

    ``question`` и ``focus`` манифест не хранит, поэтому в пересчёте они пусты.
    Это делает предикат **слабее** объявленного (состояние с непустым вопросом
    никогда не совпадёт и будет помечено осиротевшим), и слабость безопасна:
    удаляет уборка только состояния старше своего срока жизни, а пометка сама
    по себе ничего не удаляет. Обратная ошибка — считать непустой вопрос пустым
    и объявлять состояние нужным — удалила бы чужую работу.
    """
    document_path = manifest.get("document_path")
    if not document_path:
        return False
    try:
        target = safe_child(handle.subdir(FILES_SUBDIR), str(document_path))
    except PathDeniedError:
        return True
    if not target.is_file():
        return True
    length = str(manifest.get("length") or "brief")
    try:
        from libs.legal_summarizer.application.document_io import load_text

        text = load_text(target, mode="brief" if length == "brief" else "full")
    except Exception:  # noqa: BLE001 - пересчёт не обязан быть удачным
        return True
    return _compute_operation_id(
        text,
        length,
        document_path=str(document_path),
        question="",
        focus="",
    ) != operation_id


def _sweep(handle: SessionHandle, *, now: float) -> dict[str, Any]:
    """Уборка состояний старше объявленного срока жизни.

    Трогает **только** состояния старше срока: смена ``focus`` у ещё нужного
    состояния делает его осиротевшим, но не удаляет досрочно — работа по
    старому ``focus`` ещё не закончена.
    """
    import json as _json

    root = operations_dir(handle)
    if not root.is_dir():
        return {"removed": 0, "orphaned": 0}

    removed = 0
    orphaned = 0
    for operation_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        marker = _read_marker(handle, access_marker(operation_dir.name))
        manifest: dict[str, Any] = {}
        manifest_file = operation_dir / "manifest.json"
        if manifest_file.is_file():
            try:
                loaded = _json.loads(manifest_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, _json.JSONDecodeError):
                manifest = {}
        status = str(manifest.get("status") or (marker or {}).get("status") or "")
        last_access = None
        if marker is not None:
            try:
                last_access = float(marker.get("last_access_at", 0))
            except (TypeError, ValueError):
                last_access = None
        if last_access is None:
            # Состояние, к которому ни разу не обращались: возраст — от
            # начала работы, иначе оно жило бы вечно.
            last_access = 0.0
        age = now - last_access
        ttl = COMPLETE_TTL_SEC if status == "completed" else INCOMPLETE_TTL_SEC
        if age <= ttl:
            continue
        if _is_orphaned(operation_dir.name, manifest, handle):
            orphaned += 1
        if _remove_state(handle, operation_dir):
            removed += 1
    return {"removed": removed, "orphaned": orphaned}


def _remove_state(handle: SessionHandle, operation_dir: Path) -> bool:
    """Удалить каталог состояния операции вместе с её признаками."""
    artifacts = handle.subdir(ARTIFACTS_SUBDIR)
    resolved_artifacts = artifacts.resolve()
    target = operation_dir.resolve()
    # Одно состояние — один каталог внутри своей сессии. Проверка обязательна:
    # ``rmtree`` без неё снёс бы всё, до чего дотянулась неверная сборка пути.
    if resolved_artifacts != target and resolved_artifacts not in target.parents:
        logger.warning("уборка пропустила путь вне сессии: %s", target)
        return False
    for relative in (busy_marker(operation_dir.name), access_marker(operation_dir.name)):
        handle.remove(relative, subdir=ARTIFACTS_SUBDIR)
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


# ── прогресс ──────────────────────────────────────────────────────────────


def _progress_report(handle: SessionHandle, operation_id: str, outcome: dict[str, Any]) -> dict[str, Any] | None:
    """Остаток по состоянию операции.

    Условие присутствия — выполнена **хотя бы одна единица работы**, то есть в
    состоянии появилась запись чанка. Прежний маркер «домен построил контекст
    исполнения» брать нельзя: он требовал бы объявлять нули на ветке, где
    работа не начата.
    """
    report = outcome.get("progress_report")
    if isinstance(report, dict):
        return report

    from libs.legal_summarizer.cache import manifest as manifest_module

    normalized = manifest_module.load_manifest(operation_id, state_root(handle))
    if normalized is None:
        return None
    chunk_states = normalized.chunk_states or {}
    done = sum(
        1 for state in chunk_states.values() if str((state or {}).get("status")) == "completed"
    ) or len(chunk_states)
    if done <= 0 and normalized.status not in {"confirmation_required", "requires_continuation"}:
        return None
    total = normalized.chunks_total or done
    remaining = 0 if normalized.status == "completed" else max(0, total - done)
    return {
        "done": done,
        "remaining": remaining,
        "continues": normalized.status != "completed",
    }


# ── наблюдаемость ─────────────────────────────────────────────────────────


def _emit(ctx: ToolExecutionContext, writer: Any, event_type: str, *, summary: str, **payload: Any) -> None:
    """Записать событие разбора.

    Событийный писатель, а не ``log_operation``/``logger`` напрямую: имена
    проверяются по закрытому множеству ``EVENT_TYPES``, а политика неизвестных
    типов на живом контуре — ``strict``.
    """
    event = {
        "event_type": event_type,
        "summary": summary,
        "session_id": ctx.session_id,
        "user_id": ctx.user_id,
        "request_id": ctx.request_id,
        "payload": payload,
    }
    emit_events = getattr(ctx, "log_events", None)
    if callable(emit_events):
        emit_events([event])
        return
    if writer is None:
        return
    from libs.enterprise_common.eventing.models import AgentEvent

    writer.emit(AgentEvent(event_type=event_type, **{
        k: v for k, v in event.items() if k != "event_type"
    }))


# ── операция ──────────────────────────────────────────────────────────────


def create_tool(workspace: SessionWorkspace, *, execution_timeout_sec: float, writer: Any = None) -> ToolDefinition:
    """Определение операции запуска разбора.

    ``workspace`` достаётся слою исполнения и в контейнер не кладётся: держать
    его должен composition root. ``execution_timeout_sec`` — тот же
    ``execution.policy``, что ограничивает вызов конвейером, потому что срок
    жизни признака занятости выведен из него.
    """

    def handle_analyze_document(
        ctx: ToolExecutionContext,
        document: str = "",
        length: str = "brief",
        question: str = "",
        focus: str = "",
        load_mode: str = "",
        confirmed: bool = False,
        operation_id: str = "",
    ) -> str:
        """Разобрать юридический документ из вложений этой сессии.

        Аргументы:
            document: ``session://files/<путь>`` либо путь внутри ``files/``.
            length: ``brief`` или ``detailed``. Значение не из перечня —
                отказ, а не молчаливая замена на краткий формат.
            question: адресный вопрос по документу.
            focus: сузить разбор конкретной частью документа.
            load_mode: ``brief`` или ``full`` — как извлекать текст. **Обязателен**:
                выбор молча меняет объём, оценку и цену.
            confirmed: подтверждение платной работы, полученное от пользователя.
            operation_id: идентификатор состояния; проверяется, не подставляется.

        Возвращает JSON с ``status``, ``operation_id`` и ``progress_report``.
        """
        length_value = _require_enum(length, ("brief", "detailed"), "length")
        mode_value = _require_enum(load_mode, ("brief", "full"), "load_mode")

        handle = workspace.handle(ctx.session_id, create=True)
        _sweep(handle, now=time.time())

        document_path = _resolve_document(handle, document)
        text = _load_document_text(document_path, mode_value)

        question_value = (question or "").strip()
        focus_value = (focus or "").strip()
        computed_id = _compute_operation_id(
            text,
            length_value,
            document_path=str(document_path),
            question=question_value,
            focus=focus_value,
        )
        _verify_operation_id(operation_id, computed_id)

        from libs.legal_summarizer.application import service as domain

        # Признак занятости берётся ДО платной работы: новый вызов обязан
        # отказать, не сделав ни одного LLM-вызова.
        step = _take_or_refuse(
            handle,
            computed_id,
            ttl_sec=float(execution_timeout_sec) * BUSY_TTL_FACTOR,
            owner=ctx.request_id or ctx.session_id,
        )
        try:
            outcome = domain.run(
                text,
                **_accepted_kwargs(
                    domain.run,
                    length=length_value,
                    focus=focus_value or None,
                    question=question_value or None,
                    confirmed=bool(confirmed),
                    document_path=str(document_path),
                    workspace_root=state_root(handle),
                    # Ограничение обязано приходить отсюда, а не оставаться
                    # None: None — это «выполнить весь разбор», то есть ровно
                    # то поведение, из-за которого не уложившийся в потолок
                    # вызов терял оплаченную работу целиком.
                    batch_limit=_batch_budget(execution_timeout_sec),
                ),
            )
        finally:
            _release(handle, computed_id)
        # Идентичность вычислена выше, поэтому домен не должен был подставить
        # свой: подстановка молча открыла бы чужое состояние.
        outcome.setdefault("operation_id", computed_id)

        status = str(outcome.get("status") or "unknown")
        _touch(handle, computed_id, status=status)
        _emit(
            ctx,
            writer,
            EVENT_STEP,
            summary=f"разбор документа: статус={status}",
            operation_id=computed_id,
            status=status,
            length=length_value,
            load_mode=mode_value,
            step=step,
        )
        if status == "confirmation_required":
            _emit(
                ctx,
                writer,
                EVENT_CONFIRMATION,
                summary="нужно подтверждение платной работы",
                operation_id=computed_id,
            )
        elif status == "completed":
            _emit(
                ctx,
                writer,
                EVENT_COMPLETED,
                summary="разбор завершён",
                operation_id=computed_id,
            )
        elif status == "requires_continuation":
            _emit(
                ctx,
                writer,
                EVENT_PARTIAL,
                summary="разбор требует продолжения",
                operation_id=computed_id,
            )
        elif status == "failed":
            _emit(
                ctx,
                writer,
                EVENT_REFUSED,
                summary="разбор отказал",
                operation_id=computed_id,
                code=(outcome.get("error") or {}).get("code"),
            )

        payload = dict(outcome)
        report = _progress_report(handle, computed_id, outcome)
        if report is not None:
            payload["progress_report"] = report
        if status == "confirmation_required":
            # Оценка и варианты — один ответ. Ни один существующий путь не
            # отдаёт оба: `estimate` даёт run(), варианты — presenter.
            payload.update(_confirmation_options(outcome))
        if status == "requires_continuation":
            payload.setdefault(
                "progress_report",
                {
                    "done": 0,
                    "remaining": int((outcome.get("summary") or {}).get("context_batches_total") or 0),
                    "continues": False,
                },
            )
        if status == "failed" and not confirmed:
            logger.info("разбор %s отказал: %s", computed_id, payload.get("error"))
        return json.dumps(payload, ensure_ascii=False, default=str)

    description = (
        "Разобрать юридический документ из вложений этой сессии (files/) и "
        "сохранить состояние разбора в папку сессии: документ заново не "
        "разбирается, уточняющие вопросы задаются операцией "
        "platform.query_operation по возвращённому operation_id. load_mode "
        "обязателен (brief режет PDF до 100 страниц и 300 000 символов, full "
        "разбирает целиком), length — brief или detailed, значение не из "
        "перечня отвергается. Ответ требует явного confirmed: без него "
        "приходит confirmation_required с оценкой и вариантами."
    )

    definition = ToolDefinition(
        name="platform.analyze_document",
        description=description,
        handler=handle_analyze_document,
        capability="platform",
        tags=("legal_summarizer", "session"),
    )
    # Те же проверки, что и для операций из каталога: подпись без аннотаций
    # превратилась бы в пустую схему, а сессия в аргументах обошла бы подмену
    # идентичности из _meta.
    validate_handler(definition.handler, name=definition.name)
    return replace(definition, input_schema=build_input_schema(definition.handler))


def _confirmation_options(outcome: dict[str, Any]) -> dict[str, Any]:
    """Варианты краткого и подробного разбора к оценке домена."""
    from libs.legal_summarizer.output.presenter import build_confirmation_options

    estimate = outcome.get("estimate") or {}
    summary = outcome.get("summary") or {}
    try:
        return build_confirmation_options(
            chars_in=int(summary.get("chars_in") or 0),
            min_seconds=float(estimate.get("min_seconds") or 0),
            max_seconds=float(estimate.get("max_seconds") or 0),
        )
    except (TypeError, ValueError):
        return {}
