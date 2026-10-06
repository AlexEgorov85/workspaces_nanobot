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

Почему у режима загрузки нет места на поверхности. Он был обязательным и
управлял объёмом чтения: ``brief`` у PDF резал документ до 100 страниц и
300 000 символов. Структура строится **без LLM**, поэтому строить её
по-разному для краткого и подробного свода означало две разные структуры
одного файла; на длинном документе outline описывал только прочитанное начало,
и модель отвечала уверенно про документ, который видела наполовину. Объём
входа ограничивает сборка brief-чанка (окно модели и ``structure_max_chars`` на
outline), а не чтение файла.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

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

#: Подкаталог каталогов состояний внутри ``artifacts/legal_summarizer``.
#:
#: Объявлен константой, потому что имя входит в путь записи ограничения шага
#: (:func:`budget_marker`), то есть теперь у него два читателя, а не один.
OPERATIONS_DIRNAME = "operations"

#: Подкаталог надгробий убранных состояний внутри ``artifacts/`` сессии.
#:
#: Рядом с ``busy/`` и ``access/``, а не внутри ``operations/``: каталог
#: состояний перебирает :func:`_sweep`, и надгробие, лежащее среди состояний,
#: было бы вторым видом записи в одном переборе. Здесь оно рядом с признаками
#: прочие, а ``operations/`` остаётся каталогом состояний и только.
TOMBSTONES_DIRNAME = "tombstones"

#: Срок жизни надгробия.
#:
#: Убранное состояние удаляется безвозвратно, и читатель обязан отличать
#: «состояния никогда не было» от «было и убрано по сроку» — иначе модель
#: либо бесконечно повторяет заведомо пропавший ``operation_id``, либо
#: приписывает несуществовавшей работе свои собственные подробности. Значит
#: надгробие обязано прожить **не меньше** самого состояния: если оно
#: протухнет раньше, ссылка, выданная состоянием, ещё живым, приведёт к отказу
#: без объяснения.
#:
#: Два срока состояния — 24 часа и 7 суток, — берётся больший, а он удваивается:
#: надгробие живёт ещё один полный срок состояния **после** уборки. Этого хватает
#: читателю, у которого ссылка была выдана, пока состояние ещё жило: к моменту
#: уборки её возраст от последнего обращения уже не превышает
#: ``COMPLETE_TTL_SEC``, и после удаления остаётся целый срок на объяснение.
#: Без этого края объяснение исчезало бы ровно тогда, когда ссылка ещё
#: осмысленна.
#:
#: Без срока жизни каталог надгробий рос бы бесконечно — по одному файлу на
#: каждое убранное состояние за всю историю сессии. Срок есть, и он не
#: произвольный.
TOMBSTONE_TTL_SEC = 2 * COMPLETE_TTL_SEC

#: Множитель потолка вызова в сроке жизни признака занятости. Два, а не один:
#: вызов, оборванный потолком, снимает признак в ``finally`` не всегда, и
#: следующий вызов обязан иметь право его перехватить.
BUSY_TTL_FACTOR = 2

#: Доля потолка вызова, отдаваемая батчам. Остальное — загрузка документа,
#: сбор контекста исполнения, reduce-фаза и запись состояния: всё это тоже
#: идёт внутри потолка, поэтому отдавать его батчам нельзя.
BATCH_BUDGET_SHARE = 0.6

#: Нижняя граница ограничения шага. Единица — это «сделай один батч», а ноль
#: означал бы «шаг не поместился», и домен отличал бы его от «шаг не начат», то
#: есть один и тот же отказ с двумя разными причинами.
MIN_BATCH_BUDGET = 1

#: Делитель уменьшения ограничения при перехвате протухшего признака занятости.
#:
#: Два, а не «минус один батч»: повод уменьшать — факт неукладывания, а не цена
#: шага. Каждый следующий перехват уводит ограничение ещё вдвое, пока оно не
#: упрётся в :data:`MIN_BATCH_BUDGET`; ниже единицы уменьшать нечего, и виноват
#: уже не размер шага, а потолок вызова — об этом вызывающая сторона узнаёт
#: по коду отказа, а уменьшение обязано молчать.
BUDGET_BACKOFF_DIVISOR = 2

#: Имя файла ограничения шага внутри каталога состояния операции.
BUDGET_MARKER_FILENAME = "budget.json"

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
    return state_root(handle) / OPERATIONS_DIRNAME


def busy_marker(operation_id: str) -> str:
    """Относительный путь признака занятости внутри ``artifacts/``."""
    return f"{SKILL_DIRNAME}/busy/{operation_id}.json"


def access_marker(operation_id: str) -> str:
    """Относительный путь отметки последнего обращения внутри ``artifacts/``."""
    return f"{SKILL_DIRNAME}/access/{operation_id}.json"


def tombstone_marker(operation_id: str) -> str:
    """Относительный путь надгробия убранного состояния внутри ``artifacts/``.

    Объявлено рядом с остальными путями и по тому же правилу, а не собрано в
    читателе строкой: писатель и читатель обязаны приходить к одному пути,
    иначе объяснение читалось бы не по тому адресу, куда писал уборщик.
    """
    return f"{SKILL_DIRNAME}/{TOMBSTONES_DIRNAME}/{operation_id}.json"


def budget_marker(operation_id: str) -> str:
    """Относительный путь записи ограничения шага внутри ``artifacts/``.

    Запись лежит **в каталоге состояния операции**, рядом с пошаговыми файлами
    чанков, и потому умирает вместе с состоянием: уборка сносит каталог целиком,
    а надгробие объясняет пропажу обоим. Отдельным каталогом рядом с ``busy/``
    и ``access/`` она стала бы вторым местом, где надо помнить срок жизни, и
    третьим — в уборке, ради одного целого числа.

    Не в ``manifest.json``, потому что этот файл принадлежит домену:
    ``save_manifest`` пишет фиксированную схему ``NormalizedManifest.to_dict()``,
    и чужое поле молча пропало бы при следующей же записи домена, а на первом
    вызове, когда состояния ещё нет, пришлось бы выдумать манифест — и
    ``load_manifest`` прочитал бы его как чужое состояние.

    Не в отметке обращения (``access/``), потому что её перезаписывает
    ``query_operation`` при каждом чтении, записывая туда ``operation_id``,
    ``last_access_at`` и ``status`` — и больше ничего. Уменьшенное ограничение
    стёрлось бы первым же follow-up вопросом, то есть ровно тем действием,
    которое навык прямо предписывает модели делать между вызовами разбора.
    """
    return f"{SKILL_DIRNAME}/{OPERATIONS_DIRNAME}/{operation_id}/{BUDGET_MARKER_FILENAME}"


def _tombstone_payload(operation_id: str, *, now: float, orphaned: bool) -> dict[str, Any]:
    """Тело надгробия.

    Только **факт и время**: что состояние убрано, когда и почему. Удалённых
    данных здесь быть не должно — надгробие переживает само состояние, то есть
    пережило бы и его содержимое, а ответ на протухшую ссылку не должен нести
    ни куска разобранного документа. Поэтому ни текста, ни сводок, ни пути к
    документу: только идентификатор, время уборки и признак осиротевания.
    """
    return {
        "operation_id": operation_id,
        "removed_at": now,
        "removed_at_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "reason": "ttl_expired",
        "orphaned": bool(orphaned),
        "expires_at": now + TOMBSTONE_TTL_SEC,
    }


def _write_tombstone(handle: SessionHandle, operation_id: str, *, now: float, orphaned: bool) -> bool:
    """Оставить надгробие убранного состояния. Отказ — не помеха уборке.

    Надгробие — улучшение отказа, а не условие уборки: уборка обязана довести
    дело до конца даже тогда, когда надгробие записать не удалось (диск полон,
    каталог на NFS недоступен). Поэтому отказ здесь проглатывается с записью в
    журнал, а ``_remove_state`` состояние удаляет в любом случае. Обратный
    порядок — «сначала надгробие, потом удаление» — был бы хуже: сорванная
    запись надгробия оставила бы живое состояние с объявленным ему сроком,
    который уже истёк.
    """
    try:
        handle.write_json(
            tombstone_marker(operation_id),
            _tombstone_payload(operation_id, now=now, orphaned=orphaned),
            subdir=ARTIFACTS_SUBDIR,
        )
    except OSError as exc:
        logger.warning(
            "надгробие %s не записано: %s", operation_id, exc.__class__.__name__
        )
        return False
    return True


def load_tombstone(handle: SessionHandle, operation_id: str, *, now: float) -> dict[str, Any] | None:
    """Надгробие, если оно есть **и** ещё не протухло; иначе ``None``.

    Протухшее надгробие — то же, что его отсутствие: срок жизни кончился, и
    объяснение «убрано по сроку» больше не на что опереться. Возвращать его всё
    равно значило бы годами отвечать на ссылку, выданную при жизни сессии.
    """
    data = _read_marker(handle, tombstone_marker(operation_id))
    if data is None:
        return None
    try:
        expires_at = float(data.get("expires_at", 0))
    except (TypeError, ValueError):
        # Битое надгробие без срока — не объяснение: срок не проверить, а
        # отвечать по нему значило бы выдумывать основание.
        return None
    if expires_at <= now:
        return None
    return data


def _purge_tombstones(handle: SessionHandle, *, now: float) -> int:
    """Убрать протухшие надгробия.

    Без этого каталог надгробий рос бы бесконечно: уборка пишет по файлу на
    каждое убранное состояние, а вызывается она на каждом разборе. Ошибки
    чтения и снятия проглатываются по той же причине, что и в остальной
    уборке, — это гигиена каталога, а не часть контракта вызова.
    """
    directory = state_root(handle) / TOMBSTONES_DIRNAME
    if not directory.is_dir():
        return 0
    purged = 0
    for entry in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
            expires_at = float((payload or {}).get("expires_at", 0))
        except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
            # Нечитаемое надгробие срок не имеет, а значит не может быть
            # объяснением; оставлять его — значит копить мусор.
            expires_at = 0.0
        if expires_at > now:
            continue
        try:
            handle.remove(tombstone_marker(entry.stem), subdir=ARTIFACTS_SUBDIR)
        except OSError as exc:  # pragma: no cover - снятие не обязано удалиться
            logger.warning("надгробие %s не снято: %s", entry.stem, exc.__class__.__name__)
            continue
        purged += 1
    return purged


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


def _load_document_text(path: Path) -> str:
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
        text = load_text(path)
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
    ``requires_continuation`` с остатком. Это и есть :data:`MIN_BATCH_BUDGET` —
    тот же пол, на котором стоит и уменьшение ограничения.
    """
    try:
        from libs.legal_summarizer.llm.config import get_execution_config

        per_batch = float(get_execution_config().get("estimated_chunk_duration_sec") or FALLBACK_CHUNK_SEC)
    except (ImportError, AttributeError, TypeError, ValueError):
        per_batch = FALLBACK_CHUNK_SEC
    if per_batch <= 0:
        per_batch = FALLBACK_CHUNK_SEC
    usable = max(1.0, float(execution_timeout_sec) * BATCH_BUDGET_SHARE)
    return max(MIN_BATCH_BUDGET, int(usable // per_batch))


# ── идентичность ──────────────────────────────────────────────────────────


def _compute_operation_id(text: str, length: str, *, document_path: str, question: str, focus: str) -> str:
    """Идентификатор операции из документа и параметров.

    Переданный вызывающей стороной ``operation_id`` сверяется с вычисленным:
    в пределах сессии подстановка чужого значения адресовала бы чужое
    состояние, и это не «не тот аргумент», а обход границы.

    Текст нормализуется **до** хеширования так же, как это делает
    ``service.run`` (``text = (text or "").strip()``). Связь здесь жёсткая и
    не косметическая: домен берёт переданный идентификатор как есть
    (``operation_id or make_operation_id(...)``), поэтому состояние пишется
    под идентификатором домена, а отметка обращения — под вычисленным здесь.
    Расхождение в два пробела по краям давало разные значения, и уборка,
    ищущая отметку по имени каталога состояния, не находила её и удаляла
    свежее состояние — из-за чего ограниченный шаг не накапливался никогда:
    каждый вызов заново оплачивал первый батч.
    """
    from libs.legal_summarizer.application.operation_id import make_operation_id

    computed = make_operation_id(
        (text or "").strip(),
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


class BusyTake(NamedTuple):
    """Что дал захват работы: счётчик вызовов и признак перехвата.

    Два факта, а не один. По ``step`` видно, сколько раз работа бралась, а
    ``recovered_after_timeout`` отвечает на вопрос, который изнутри вызова
    не виден вовсе: уложился ли прежний поток в потолок.

    Почему перехват и есть наблюдаемый недобор. Поток, не уложившийся в потолок,
    **не прерывается** (``execution/pipeline.py`` — ``future.cancel()`` для идущей
    задачи не действует), и его возврат отбрасывается целиком, поэтому вызов
    таймаута не возвращает ничего: ни ответа, ни отказа, ни записи состояния.
    Зато признак занятости такой поток удерживает до своего ``finally``, и
    следующий вызов получает ``operation_in_progress`` вместо дублирования
    идущей работы (спека ``skills/legal-summarizer-query``, сценарий «Поток
    пережил таймаут»). Дождавшись истечения срока признака
    (``execution_timeout_sec × :data:`BUSY_TTL_FACTOR` ``), этот следующий вызов
    берёт работу вместо прежнего владельца — и момент перехвата оказывается
    единственным следом неукладывания, который вообще остаётся на диске.

    Нечитаемый признак перехватом **не** считается: что именно он был и чей —
    неизвестно, а счётчик в этом случае начинается с единицы. Уменьшать
    ограничение по битому файлу значило бы взимать плату за чужую порчу.
    """

    step: int
    recovered_after_timeout: bool


def _take_or_refuse(
    handle: SessionHandle, operation_id: str, *, ttl_sec: float, owner: str
) -> BusyTake:
    """Решение о работе принимается **до** платной работы.

    Новый вызов читает ``step`` и ``session_token``; срок жизни признака не
    истёк — отказ ``operation_in_progress``, не сделав ни одного LLM-вызова.
    Истёк — работа берётся, счётчик продолжает расти, и оплаченное не
    оплачивается повторно; заодно возвращается признак перехвата, по
    которому :func:`_resolve_batch_budget` уменьшает ограничение.
    """
    now = time.time()
    existing = _read_marker(handle, busy_marker(operation_id))
    step = 0
    recovered = False
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
        # Признак дожил до конца своего срока, не будучи снятым: значит прежний
        # поток его удерживал и до конца не дошёл, то есть не уложился в потолок.
        recovered = True
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
    return BusyTake(step=step, recovered_after_timeout=recovered)


def _release(handle: SessionHandle, operation_id: str) -> None:
    """Признак снимается в ``finally`` вызова."""
    try:
        handle.remove(busy_marker(operation_id), subdir=ARTIFACTS_SUBDIR)
    except OSError:  # pragma: no cover - снятие признака не обязано удаться
        logger.warning("признак занятости %s не снят", operation_id)


# ── ограничение шага в состоянии операции ─────────────────────────────────
#
# Перенос ограничения на следующий вызов — вторая половина «ограничение
# помещается в потолок». Расчётный бюджет считается заново на каждом вызове
# (:func:`_batch_budget`), а уменьшение обязано пережить вызов, который не
# уложился, — иначе отказ повторялся бы по кругу от одного и того же значения.
# Поэтому применённое ограничение пишется в состояние операции (путь и
# доводы в :func:`budget_marker`) и читается следующим вызовом.


def _stored_batch_budget(handle: SessionHandle, operation_id: str) -> int | None:
    """Применённое ограничение прошлого вызова; ``None`` — считать заново.

    ``None`` означает «ограничения нет»: записи ещё не было, она сброшена
    завершением разбора или негодна. Негодная запись — отсутствующая,
    нечитаемая, не целая или неположительная — не должна ронять вызов: она
    означает лишь, что считать надо заново, то есть вернуться к потолку.
    """
    stored = _read_marker(handle, budget_marker(operation_id))
    if stored is None:
        return None
    try:
        budget = int(stored["batch_budget"])
    except (KeyError, TypeError, ValueError):
        return None
    return budget if budget >= MIN_BATCH_BUDGET else None


def _resolve_batch_budget(
    handle: SessionHandle,
    operation_id: str,
    *,
    execution_timeout_sec: float,
    recovered_after_timeout: bool,
) -> int:
    """Ограничение шага этого вызова.

    Порядок правил, и он не случаен:

    * **сверху** — расчётный потолок вызова. Записанное значение считалось под
      прежними настройками, а потолок и оценка батча могут уменьшиться, и
      ограничение, не помещающееся в потолок, хуже его отсутствия: оно
      выглядит как защита ровно там, где защиты нет;
    * **снизу и вдвое** — если вызов взял работу по истёкшему сроку признака
      занятости, то есть прежний поток не уложился (основание — доводы в
      :class:`BusyTake`). Делитель — :data:`BUDGET_BACKOFF_DIVISOR`, пол —
      :data:`MIN_BATCH_BUDGET`, тот же, что и у расчётного бюджета: ноль
      означал бы «шаг не поместился» вместо «шаг не начат».

    Без перехвата записанное ограничение сохраняется как есть и **не растёт**:
    вызов, который уложился, не даёт повода вернуть потолок, а рост обратно
    означал бы, что одно неукладывание стоит одного и того же отказа снова и
    снова. Единственное место, где ограничение снимается, — завершённый
    разбор: работа закончена, и следующий разбор начинается заново.
    """
    ceiling = _batch_budget(execution_timeout_sec)
    stored = _stored_batch_budget(handle, operation_id)
    budget = ceiling if stored is None else min(ceiling, stored)
    if not recovered_after_timeout:
        return budget
    return max(MIN_BATCH_BUDGET, budget // BUDGET_BACKOFF_DIVISOR)


def _remember_batch_budget(handle: SessionHandle, operation_id: str, budget: int | None) -> None:
    """Записать применённое ограничение; ``None`` — сбросить его.

    Сброс пишется, а не удаляется файлом: чтение «ограничения нет» не должно
    зависеть от того, сложилось ли удаление, а явная запись читается и как
    «сброшено», и как «ещё не применялось» — оба состояния означают одно и то
    же: считать от потолка.
    """
    _write_marker(
        handle,
        budget_marker(operation_id),
        {
            "operation_id": operation_id,
            "batch_budget": budget,
            "recorded_at": time.time(),
        },
    )


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


def _document_from_manifest(handle: SessionHandle, document_path: str) -> Path | None:
    """Путь документа из манифеста — ``None``, если он вне каталога сессии.

    В боевой форме путь в манифесте **абсолютный**: домен пишет
    ``document_path=str(document_path)``, а операция передаёт ему путь, который
    разрешила сама (``_resolve_document`` → ``safe_child``). Раньше предикат
    скормил этот путь в ``safe_child`` и получал ``PathDeniedError``, то есть
    объявлял осиротевшим **любое** состояние, включая то, чей идентификатор
    совпадает с пересчитанным. Признак в итоге всегда был ``true``, и по нему
    перестало быть видно, подменён ли документ или просто истёк срок, — а это
    ровно то различение, ради которого предикат написан.

    Граница проверяется явно, а не отказом от абсолютного пути: путь внутри
    ``files/`` законен, путь снаружи — нет. Отказ от формы был бы запретом на
    боевую форму манифеста, то есть на собственную же запись.
    """
    files_root = handle.subdir(FILES_SUBDIR).resolve()
    candidate = Path(document_path)
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not resolved.is_relative_to(files_root):
                return None
            return resolved
        return safe_child(files_root, document_path)
    except (PathDeniedError, OSError, ValueError):
        return None


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
    target = _document_from_manifest(handle, str(document_path))
    if target is None:
        return True
    if not target.is_file():
        return True
    length = str(manifest.get("length") or "brief")
    try:
        from libs.legal_summarizer.application.document_io import load_text

        # Извлечение одно для обоих режимов: пересчёт обязан получить тот же
        # текст, что и при разборе, иначе предикат сравнивал бы хеши разных
        # извлечений и объявлял осиротевшим чужую работу.
        text = load_text(target)
    except Exception:  # noqa: BLE001 - пересчёт не обязан быть удачным
        return True
    return _compute_operation_id(
        (text or "").strip(),
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

    Протухшие надгробия убираются здесь же, а не отдельным проходом: уборка
    вызывается на каждом разборе, то есть это единственная точка, которая
    точно срабатывает, и отдельный вызов «пора бы почистить» рано или поздно
    перестал бы вызываться.
    """
    import json as _json

    _purge_tombstones(handle, now=now)
    root = operations_dir(handle)
    if not root.is_dir():
        return {"removed": 0, "orphaned": 0}

    removed = 0
    orphaned_count = 0
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
        orphaned = _is_orphaned(operation_dir.name, manifest, handle)
        if orphaned:
            orphaned_count += 1
        if _remove_state(handle, operation_dir, now=now, orphaned=orphaned):
            removed += 1
    return {"removed": removed, "orphaned": orphaned_count}


def _remove_state(
    handle: SessionHandle,
    operation_dir: Path,
    *,
    now: float,
    orphaned: bool = False,
) -> bool:
    """Удалить каталог состояния операции вместе с её признаками.

    Надгробие пишется **после** успешного удаления и только тогда, когда
    каталог действительно исчез: надгробие о состоянии, которое осталось на
    диске, вводило бы в заблуждение сильнее его отсутствия — читатель объяснил
    бы протухание там, где состояние живо и его нужно продолжать.

    Признаки занятости и обращения снимаются **до** удаления каталога, как и
    прежде: они лежат в ``busy/`` и ``access/``, а не внутри каталога
    состояния, и их удаление не зависит от того, состояние это или нет.
    """
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
    if target.exists():
        return False
    _write_tombstone(handle, operation_dir.name, now=now, orphaned=orphaned)
    return True


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
            confirmed: подтверждение платной работы, полученное от пользователя.
            operation_id: идентификатор состояния; проверяется, не подставляется.

        Документ читается целиком, структура строится один раз и от режима
        не зависит: различаются своды, а не объём прочитанного. Отдельного
        параметра извлечения не было и в релизе 2.5.3 — там он был выведен из
        ``length``, но для краткого свода это резало PDF до 100 страниц, и
        outline описывал только начало документа. Краткий свод ограничивает
        сборка brief-чанка (окно модели плюс ``structure_max_chars`` на
        outline), а не чтение файла.

        Возвращает JSON с ``status``, ``operation_id`` и ``progress_report``.
        """
        length_value = _require_enum(length, ("brief", "detailed"), "length")

        handle = workspace.handle(ctx.session_id, create=True)
        _sweep(handle, now=time.time())

        document_path = _resolve_document(handle, document)
        text = _load_document_text(document_path)

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
        from libs.legal_summarizer.llm import config as domain_config

        # Признак занятости берётся ДО платной работы: новый вызов обязан
        # отказать, не сделав ни одного LLM-вызова.
        taken = _take_or_refuse(
            handle,
            computed_id,
            ttl_sec=float(execution_timeout_sec) * BUSY_TTL_FACTOR,
            owner=ctx.request_id or ctx.session_id,
        )
        try:
            batch_limit = _resolve_batch_budget(
                handle,
                computed_id,
                execution_timeout_sec=execution_timeout_sec,
                recovered_after_timeout=taken.recovered_after_timeout,
            )
            # Применённое ограничение записывается **до** платной работы:
            # вызов, не уложившийся в потолок, не вернётся вовсе и не запишет
            # после себя ничего, а уменьшение обязано пережить именно такой
            # вызов. Запись после него стёрла бы саму причину уменьшения.
            _remember_batch_budget(handle, computed_id, batch_limit)
            # Личность оборота привязывается на время доменной работы и
            # снимается сразу после. Домен читает её сам, из
            # ``llm.config.get_identity()``, и уходит с ней в ``llm.complete``
            # как ``params._meta``; без привязки она пуста, платформа отвечает
            # ``identity_missing``, домен глотает отказ в
            # ``REDUCE_INPUT_EMPTY``, и разбор **любого** настоящего документа
            # заканчивается отказом — при зелёных юнит-пробах, потому что они
            # домен не подменяют, а значит и в LLM не ходят.
            #
            # Привязка контекстная (``using_identity``), а не записью в
            # конфигурацию: обслуживает оборотов несколько, и глобальная
            # подстановка смешала бы личности параллельных разборов в журнале.
            #
            # Ключи — **простые имена**, а не ``as_meta()``: домен читает их
            # сам, ``llm/client.py::_identity`` ищет ``session_id`` и
            # ``user_id`` без префикса, и пространственные ``workspaces/*``
            # молча не нашлись бы — личность выглядела бы привязанной, а на
            # провод ушла бы пустой. Префикс нужен только на стороне платформы,
            # там, где из этих имён снова собирается ``McpCallContext``.
            with domain_config.using_identity(
                {
                    "session_id": ctx.call.session_id,
                    "user_id": ctx.call.user_id,
                    "request_id": ctx.call.request_id,
                }
            ):
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
                        batch_limit=batch_limit,
                        # Потолок вызова передаётся домену тем же способом, что и
                        # остальные именованные аргументы: по имени, а не по
                        # подписи конкретной версии. Отбор отбросил бы значение
                        # молча, только если параметр уберут из ``service.run`` —
                        # тогда вызов перестанет ограничивать неделимый шаг, и это
                        # должен заметить страж на подсаженном дефекте, а не
                        # владелец по счастливой случайности.
                        call_budget_sec=execution_timeout_sec,
                    ),
                )
        finally:
            _release(handle, computed_id)
        # Идентичность вычислена выше, поэтому домен не должен был подставить
        # свой: подстановка молча открыла бы чужое состояние.
        outcome.setdefault("operation_id", computed_id)

        status = str(outcome.get("status") or "unknown")
        _touch(handle, computed_id, status=status)
        if status == "completed":
            # Разбор закончен: уменьшенное ограничение впереди не нужно, и
            # следующий разбор этого документа начинается с полного потолка.
            # Это единственное место, где ограничение снимается, — при
            # незавершённом состоянии оно не растёт само, иначе отказ после
            # одного неукладывания повторялся бы по кругу.
            _remember_batch_budget(handle, computed_id, None)
        _emit(
            ctx,
            writer,
            EVENT_STEP,
            summary=f"разбор документа: статус={status}",
            operation_id=computed_id,
            status=status,
            length=length_value,
            step=taken.step,
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
        "platform.query_operation по возвращённому operation_id. Документ "
        "читается целиком, структура строится один раз. length — brief или "
        "detailed, значение не из перечня отвергается: brief — один "
        "структурный chunk (схема документа плюс выжимки разделов) и один "
        "проход, detailed — весь документ по чанкам и полный разбор. Ответ "
        "требует явного confirmed: без него приходит confirmation_required с "
        "оценкой и вариантами."
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
