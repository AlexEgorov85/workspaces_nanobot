"""Уборка состояния операции: срок жизни, осиротевание, надгробия.

Пробы к задаче 5.20 плана ``2026-10-05-legal-summarizer-session-scope``.
Сверху уборка уже закрыта (``test_expired_operation_state.py``: протухшая
``session://``-ссылка объясняется надгробием). Здесь то, что сверху не видно:

* срок **незавершённого** состояния (24 часа) и **завершённого** (7 суток) —
  каждая половина в обе стороны: моложе срока состояние остаётся и читается,
  старше — удаляется;
* осьротевание как **пометка**, а не как повод удалить досрочно: состояние, чей
  ``operation_id`` больше не вычисляется, живёт до объявленного срока;
* граница ``_remove_state``: каталог вне сессии не сносится;
* надгробие: срок длиннее любого срока состояния, протухшие снимаются, а тело
  не переживает содержимое удалённого состояния.

Время задаётся явно — ``now`` уходит в ``_sweep`` аргументом, — поэтому проба
срока не ждёт ``time.time()`` и не зависит от машинных часов.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import EnterpriseError  # noqa: E402
from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext,
    ToolExecutionContext,
)
from libs.enterprise_common.session.workspace import (  # noqa: E402
    SessionHandle,
    SessionWorkspace,
)
from libs.legal_summarizer.application.document_io import load_text  # noqa: E402
from libs.legal_summarizer.application.operation_id import (  # noqa: E402
    make_operation_id,
)
from libs.legal_summarizer.cache.manifest import load_manifest  # noqa: E402
from servers.enterprise.tools import analyze_document as ad  # noqa: E402
from servers.enterprise.tools import query_operation as qo  # noqa: E402

#: Фиксированный момент уборки: ``NOW - 24 * 3600`` не зависит от часов машины.
NOW = 1_700_000_000.0

SESSION_ID = "sess-cleanup"

#: Документ в ``files/`` сессии. По умолчанию в манифесте путь
#: **относительный** (``doc.txt``): предикат осиротевания пересчитывает
#: идентификатор по пути внутри сессии.
#:
#: Это форма большинства проб, а **не** единственная, которую предикат способен
#: прочитать: боевой манифест домен пишет с **абсолютным** путём, и обе формы
#: покрыты ниже — ``test_absolute_path_from_the_production_manifest_is_not_orphaned``
#: и два соседних. Прежние пробы строили манифест только с относительной,
#: поэтому дефект, объявлявший осиротевшим любое состояние, прошёл ревью
#: незамеченным: на единственной покрытой форме предикат работал верно.
DOC_NAME = "doc.txt"
DOC_TEXT = "Статья 1. Предмет договора.\nСтатья 2. Порядок расчётов.\n"


# ── прибор ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


@pytest.fixture()
def handle(workspace: SessionWorkspace) -> SessionHandle:
    return workspace.handle(SESSION_ID, create=True)


def _document(handle: SessionHandle, name: str = DOC_NAME, text: str = DOC_TEXT) -> Path:
    """Положить документ в ``files/`` сессии, как это делает агент."""
    path = handle.subdir(ad.FILES_SUBDIR) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _read_document(path: Path) -> str:
    """Текст, который операция кладёт в идентичность, — результат ``load_text``.

    Идентичность считается по тексту **после** извлечения, а не по байтам файла
    (операция хеширует то, что вернул загрузчик, и переводы строк на этом шаге
    меняются). Поэтому и ожидаемый идентификатор пробы, и пересчёт уборки берут
    один и тот же ``load_text``: идентификатор, построенный по сырому файлу,
    расходился бы с пересчётом на ``\\n`` против ``\\r\\n`` и краснел бы без
    причины.
    """
    return load_text(path, mode="brief")


def _operation_id(
    *,
    text: str = DOC_TEXT,
    length: str = "brief",
    document_path: str = DOC_NAME,
    question: str | None = None,
    focus: str | None = None,
) -> str:
    """Идентификатор состояния — доменной функцией, а не той, что под пробой.

    Идентичность в платформе одна (``application/operation_id.py``), и проба,
    строящая ожидаемое значение **ею же**, проверяла бы согласие функции с
    собой, а не с объявленным правилом.
    """
    return make_operation_id(
        text.strip(), length, document_path=document_path, question=question, focus=focus
    )


def _write_manifest(
    directory: Path,
    *,
    operation_id: str,
    status: str,
    document_path: str | None,
    length: str = "brief",
    chars_in: int | None = None,
    chunks_total: int = 2,
) -> Path:
    """Записать ``manifest.json`` формы v2 — как это делает домен.

    Набор полей повторяет ``cache.manifest.save_manifest``: манифест должен быть
    читаем и предикатом уборки (сырой ``json``), и доменным читателем
    ``load_manifest`` (версия 2).
    """
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "operation_id": operation_id,
        "status": status,
        "document_path": document_path,
        "length": length,
        "chars_in": len(DOC_TEXT) if chars_in is None else chars_in,
        "chunks_total": chunks_total,
        "context_batches_total": 1,
        "chunk_states": {
            f"chunk-{index}": {"status": "completed" if status == "completed" else "running"}
            for index in range(chunks_total)
        },
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _access_marker(
    handle: SessionHandle,
    operation_id: str,
    last_access_at: Any,
    *,
    status: str,
) -> None:
    """Отметка последнего обращения — ровно то, что пишет ``_touch``."""
    handle.write_json(
        ad.access_marker(operation_id),
        {"operation_id": operation_id, "last_access_at": last_access_at, "status": status},
        subdir=ad.ARTIFACTS_SUBDIR,
    )


def _state(
    handle: SessionHandle,
    *,
    operation_id: str,
    status: str,
    document_path: str | None,
    last_access_at: float | None,
    length: str = "brief",
) -> Path:
    """Состояние операции: каталог ``operations/<id>/`` плюс отметка обращения."""
    directory = ad.operations_dir(handle) / operation_id
    _write_manifest(
        directory,
        operation_id=operation_id,
        status=status,
        document_path=document_path,
        length=length,
    )
    if last_access_at is not None:
        _access_marker(handle, operation_id, last_access_at, status=status)
    return directory


def _tombstone(handle: SessionHandle, operation_id: str) -> dict[str, Any]:
    return json.loads(
        handle.read_text(ad.tombstone_marker(operation_id), subdir=ad.ARTIFACTS_SUBDIR)
    )


def _tombstone_exists(handle: SessionHandle, operation_id: str) -> bool:
    return handle.exists(ad.tombstone_marker(operation_id), subdir=ad.ARTIFACTS_SUBDIR)


def _ctx(tool_name: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id="req-cleanup", session_id=SESSION_ID, user_id="u-1"
        ),
        tool_name=tool_name,
        capability="platform",
        started_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
    )


def _query(workspace: SessionWorkspace, operation_id: str, *, field: str = "stats") -> dict:
    """Спросить состояние так, как спросит модель: операцией ``query_operation``."""
    definition = qo.create_tool(workspace)
    return json.loads(definition.handler(_ctx("platform.query_operation"), operation_id, field))


# ── сроки жизни ────────────────────────────────────────────────────────────


def test_declared_ttls_are_24_hours_and_7_days() -> None:
    """Задача объявляет срок числами, а не «по вкусу».

    Краснеет, если ``INCOMPLETE_TTL_SEC`` сократить до часа или
    ``COMPLETE_TTL_SEC`` взять равным незавершённому: остальные пробы считают
    возраст от этих констант и вслед за их правкой поедут вместе с ними, то
    есть объявленный срок перестанет быть проверяемым вовсе.
    """
    assert ad.INCOMPLETE_TTL_SEC == 24 * 60 * 60
    assert ad.COMPLETE_TTL_SEC == 7 * 24 * 60 * 60


def test_incomplete_state_is_removed_after_its_ttl(handle: SessionHandle) -> None:
    """Незавершённое состояние, к которому не было обращения сутки, удаляется.

    Возраст — 24 часа и минута. Краснеет, если для незавершённого взят срок
    завершённого (тогда состояние переживёт уборку и будет удалено лишь через
    неделю) или если ``_remove_state`` не доводит дело до конца.
    """
    operation_id = _operation_id()
    _document(handle)
    directory = _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=NOW - ad.INCOMPLETE_TTL_SEC - 60,
    )

    report = ad._sweep(handle, now=NOW)

    assert report["removed"] == 1
    assert not directory.exists()
    assert load_manifest(operation_id, ad.state_root(handle)) is None


def test_incomplete_state_touched_recently_stays_and_can_continue(
    workspace: SessionWorkspace, handle: SessionHandle
) -> None:
    """Живое состояние остаётся и по-прежнему продолжается.

    Возраст — 24 часа минус минута. Краснеет, если срок посчитан от чего-то
    другого (от ``started_at``, от даты записи манифеста) или урезан: состояние
    исчезло бы посреди работы, и следующий вызов `analyze_document` молча
    начал бы разбор заново.
    """
    operation_id = _operation_id()
    _document(handle)
    directory = _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=NOW - (ad.INCOMPLETE_TTL_SEC - 60),
    )

    report = ad._sweep(handle, now=NOW)

    assert report["removed"] == 0
    assert directory.is_dir()
    answer = _query(workspace, operation_id)
    assert answer["status"] == "requires_continuation"
    assert answer["ready"] is False
    assert answer["progress_report"]["continues"] is True


def test_completed_state_survives_just_under_seven_days(handle: SessionHandle) -> None:
    """Завершённый разбор не убирается на сутки.

    Возраст — 7 суток минус минута. Краснеет, если для завершённого взят срок
    незавершённого: готовый разбор, за которым ещё ходят за статьями, исчез бы
    через сутки вместо недели.
    """
    operation_id = _operation_id()
    directory = _state(
        handle,
        operation_id=operation_id,
        status="completed",
        document_path=None,
        last_access_at=NOW - (ad.COMPLETE_TTL_SEC - 60),
    )

    report = ad._sweep(handle, now=NOW)

    assert report["removed"] == 0
    assert directory.is_dir()


def test_completed_state_is_removed_after_seven_days(handle: SessionHandle) -> None:
    """Завершённый разбор убирается по своему сроку, а не живёт вечно.

    Возраст — 7 суток и минута. Краснеет, если завершённые состояния вообще не
    убираются (например, проверка статуса ищет ``"done"``, а не ``"completed"``),
    и тогда каталог сессии растёт без срока.
    """
    operation_id = _operation_id()
    directory = _state(
        handle,
        operation_id=operation_id,
        status="completed",
        document_path=None,
        last_access_at=NOW - (ad.COMPLETE_TTL_SEC + 60),
    )

    report = ad._sweep(handle, now=NOW)

    assert report["removed"] == 1
    assert not directory.exists()


def test_request_after_expiry_reports_the_state_is_gone(
    workspace: SessionWorkspace, handle: SessionHandle
) -> None:
    """После уборки запрос к состоянию даёт отказ, а не пустую сводку.

    Краснеет, если уборка не удалила состояние (тогда запрос отвечает данными
    удалённой работы) или если читатель не отличает убранное по сроку от
    «состояния никогда не было» — модель обязана получить отказ, а не ответ
    вида ``ok`` из ничего.
    """
    operation_id = _operation_id()
    _document(handle)
    _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=NOW - ad.INCOMPLETE_TTL_SEC - 60,
    )
    ad._sweep(handle, now=NOW)

    assert load_manifest(operation_id, ad.state_root(handle)) is None
    with pytest.raises(EnterpriseError) as refusal:
        _query(workspace, operation_id)

    assert refusal.value.code == "not_found"
    assert operation_id in str(refusal.value)


@pytest.mark.parametrize(
    "marker",
    ["нет отметки", "битая отметки"],
    ids=["без-отметки-обращения", "битая-отметка-обращения"],
)
def test_state_without_usable_access_marker_is_swept(
    handle: SessionHandle, marker: str
) -> None:
    """Состояние, к которому ни разу не обращались, убирается, а не живёт вечно.

    Отметки либо нет вовсе, либо её ``last_access_at`` — не число. Краснеет, если
    уборка перестанет считать возраст от начала работы: ``age = now - None``
    поднимет ``TypeError`` и **вся** уборка разобьётся на каждом разборе, а
    «мягкий» вариант (пропустить состояние) оставит его навсегда.
    """
    operation_id = _operation_id()
    directory = _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=None,
    )
    if marker == "битая отметки":
        _access_marker(handle, operation_id, "не-число", status="requires_continuation")

    report = ad._sweep(handle, now=NOW)

    assert report["removed"] == 1
    assert not directory.exists()


# ── осьротевание ────────────────────────────────────────────────────────────


def test_state_whose_id_recomputes_is_not_orphaned(handle: SessionHandle) -> None:
    """Идентификатор вычисляется заново и совпадает — состояние не осиротело.

    Краснеет, если предикат объявит осиротевшим всё подряд (например, вернёт
    ``True`` по умолчанию или откажется разбирать путь): тогда счётчик
    ``orphaned`` и признак в надгробии перестают значить что-либо, и по ним
    больше нельзя отличить подменённый документ от обычного истечения срока.
    """
    path = _document(handle)
    operation_id = _operation_id(text=_read_document(path))
    manifest = {"document_path": DOC_NAME, "length": "brief"}

    assert ad._is_orphaned(operation_id, manifest, handle) is False


def test_state_whose_document_was_replaced_is_orphaned(handle: SessionHandle) -> None:
    """Документ заменён содержимым — ``operation_id`` больше не вычисляется.

    Краснеет, если предикат объявит осиротевшим только исчезнувший файл: подменённый
    документ — тоже осиротевшее состояние, и это единственный признак, по
    которому уборка отличает «никто не придёт» от «придут, но за другим».
    """
    path = _document(handle)
    operation_id = _operation_id(text=_read_document(path))
    path.write_text("Статья 9. Ответственность сторон.\n", encoding="utf-8")
    manifest = {"document_path": DOC_NAME, "length": "brief"}

    assert ad._is_orphaned(operation_id, manifest, handle) is True


#: В манифесте боевого прогона ``document_path`` **абсолютный**: домен пишет
#: ``document_path=str(document_path)``, а операция передаёт ему путь, который
#: разрешила сама. Предикат, скормивший его в ``safe_child`` (который абсолютный
#: путь отвергает), объявлял осиротевшим любое состояние, включая своё.
def test_absolute_path_from_the_production_manifest_is_not_orphaned(
    handle: SessionHandle,
) -> None:
    """Абсолютный путь, идентификатор совпадает — состояние не осиротело.

    Краснеет на прежней форме предиката: ``safe_child`` отвергает абсолютный
    путь, и предикат возвращал ``True`` для состояния, чей идентификатор
    совпадает с пересчитанным. Признак тогда всегда ``true``, и по нему
    перестаёт быть видно, подменён ли документ или просто истёк срок.
    """
    path = _document(handle)
    absolute = str(path)
    operation_id = _operation_id(text=_read_document(path), document_path=absolute)
    manifest = {"document_path": absolute, "length": "brief"}

    assert ad._is_orphaned(operation_id, manifest, handle) is False


def test_replaced_document_under_absolute_path_is_orphaned(
    handle: SessionHandle,
) -> None:
    """Подмена документа ловится и на абсолютном пути."""
    path = _document(handle)
    absolute = str(path)
    operation_id = _operation_id(text=_read_document(path), document_path=absolute)
    path.write_text("Статья 9. Ответственность сторон.\n", encoding="utf-8")
    manifest = {"document_path": absolute, "length": "brief"}

    assert ad._is_orphaned(operation_id, manifest, handle) is True


def test_absolute_path_outside_the_session_is_orphaned(
    handle: SessionHandle, tmp_path: Path
) -> None:
    """Абсолютный путь границу не отменяет: вне ``files/`` — осиротело.

    Проверка обязательна рядом с предыдущими: разрешение абсолютного пути
    внутри сессии не должно становиться поблажкой для любого абсолютного пути.
    """
    outside = tmp_path / "elsewhere" / "doc.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(DOC_TEXT, encoding="utf-8")
    operation_id = _operation_id(document_path=str(outside))
    manifest = {"document_path": str(outside), "length": "brief"}

    assert ad._is_orphaned(operation_id, manifest, handle) is True


def test_changed_focus_state_stays_until_its_ttl_then_goes(handle: SessionHandle) -> None:
    """Смена ``focus`` — повод пометить состояние, а не удалить его досрочно.

    Два шага одним состоянием, как и объявлено: пока обращение было недавно,
    работа по старому ``focus`` ещё не закончена и каталог остаётся целым; после
    срока то же состояние убирается и в надгробии попадает ``orphaned: true``.
    Краснеет, если уборка удаляет осиротевшее раньше срока (шаг «осталось») или
    пропускает его после срока (шаг «удалено по истечении срока»).
    """
    path = _document(handle)
    operation_id = _operation_id(text=_read_document(path), focus="статья 5")
    directory = _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=NOW - 60,
    )

    fresh = ad._sweep(handle, now=NOW)

    assert fresh["removed"] == 0
    assert directory.is_dir()

    _access_marker(
        handle, operation_id, NOW - ad.INCOMPLETE_TTL_SEC - 60, status="requires_continuation"
    )
    expired = ad._sweep(handle, now=NOW)

    assert expired["removed"] == 1
    assert expired["orphaned"] == 1
    assert not directory.exists()
    assert _tombstone(handle, operation_id)["orphaned"] is True


# ── снятие состояния ───────────────────────────────────────────────────────


def test_markers_are_removed_together_with_the_state(handle: SessionHandle) -> None:
    """Признаки занятости и обращения снимаются вместе с каталогом состояния.

    Краснеет, если уборка сносит только каталог: отметка обращения переживёт
    состояние, и следующий разбор получит ``last_access_at`` от несуществующей
    работы — то есть уборка станет выглядеть работающей, а состояния будут
    копиться.
    """
    operation_id = _operation_id()
    _document(handle)
    _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=NOW - ad.INCOMPLETE_TTL_SEC - 60,
    )
    handle.write_json(
        ad.busy_marker(operation_id),
        {"operation_id": operation_id, "step": 3, "expires_at": NOW + 60},
        subdir=ad.ARTIFACTS_SUBDIR,
    )

    ad._sweep(handle, now=NOW)

    assert not handle.exists(ad.access_marker(operation_id), subdir=ad.ARTIFACTS_SUBDIR)
    assert not handle.exists(ad.busy_marker(operation_id), subdir=ad.ARTIFACTS_SUBDIR)


def test_remove_state_refuses_a_directory_outside_the_session(
    handle: SessionHandle, tmp_path: Path
) -> None:
    """Каталог вне сессии уборка не сносит, даже получив его прямо в руки.

    Краснеет, если убрать проверку вложенности в ``_remove_state``: ``rmtree``
    без неё снёс бы всё, до чего дотянулась неверная сборка пути, — то есть
    уборка одного состояния стала бы способом удалить произвольный каталог.
    """
    outside = tmp_path / "outside" / "op_outside"
    (outside / "chunks").mkdir(parents=True)
    (outside / "chunks" / "chunk-0.json").write_text("{}", encoding="utf-8")

    removed = ad._remove_state(handle, outside, now=NOW, orphaned=False)

    assert removed is False
    assert (outside / "chunks" / "chunk-0.json").is_file()
    assert not _tombstone_exists(handle, "op_outside")


def test_sweep_runs_on_a_real_parse_call(
    workspace: SessionWorkspace, handle: SessionHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Уборка живёт на стороне операции и срабатывает на каждом разборе.

    Домен уборки не имеет, поэтому единственная точка, которая точно
    срабатывает, — вызов ``analyze_document``. Краснеет, если убрать
    ``_sweep`` из ручки: состояния, протухшие без единого нового разбора,
    остались бы лежать до следующего разбора — то есть срок держался бы на
    руках у того, кто случайно зайдёт.
    """
    from libs.legal_summarizer.application import service as domain

    _document(handle)
    stale_id = _operation_id(text="Статья 7. Расторжение.\n")
    stale = _state(
        handle,
        operation_id=stale_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=time.time() - ad.INCOMPLETE_TTL_SEC - 60,
    )

    written: list[str] = []

    def fake_run(
        text: str,
        *,
        length: str = "brief",
        focus: str | None = None,
        question: str | None = None,
        confirmed: bool = False,
        document_path: str | None = None,
        workspace_root: Any = None,
    ) -> dict[str, Any]:
        """Домен без LLM: пишет состояние ровно тем путём, каким пишет домен.

        Параметры объявлены **по именам**, а не через ``**kwargs``: операция
        передаёт домену ограничение шага по именам (``_accepted_kwargs``), и
        заглушка, принимающая только ``kwargs``, отфильтровала бы всё и упала
        бы на ``workspace_root``.
        """
        operation_id = make_operation_id(
            text.strip(),
            length,
            document_path=document_path,
            question=question,
            focus=focus,
        )
        _write_manifest(
            Path(workspace_root) / "operations" / operation_id,
            operation_id=operation_id,
            status="completed",
            document_path=document_path,
            length=length,
        )
        written.append(operation_id)
        return {
            "status": "completed",
            "operation_id": operation_id,
            "summary": {"chars_in": len(text)},
        }

    monkeypatch.setattr(domain, "run", fake_run)
    definition = ad.create_tool(workspace, execution_timeout_sec=30.0)

    definition.handler(
        _ctx("platform.analyze_document"),
        document=DOC_NAME,
        length="brief",
        confirmed=True,
    )

    assert not stale.exists()
    assert written and (ad.operations_dir(handle) / written[0]).is_dir()


# ── надгробия ──────────────────────────────────────────────────────────────


def test_tombstone_lives_longer_than_any_state_ttl() -> None:
    """Надгробие живёт ещё один полный срок состояния **после** уборки.

    Краснеет, если ``TOMBSTONE_TTL_SEC`` опустить до ``COMPLETE_TTL_SEC`` или
    меньше: ссылка, выданная состоянием, ещё живым, привела бы к отказу без
    объяснения ровно тогда, когда она ещё осмысленна.
    """
    assert ad.TOMBSTONE_TTL_SEC > ad.COMPLETE_TTL_SEC
    assert ad.TOMBSTONE_TTL_SEC >= ad.INCOMPLETE_TTL_SEC


def test_expired_tombstone_is_purged_and_live_one_is_kept(handle: SessionHandle) -> None:
    """Протухшее надгробие снимается уборкой, живое — остаётся.

    Краснеет, если чистка протухших пропущена (каталог надгробий растёт вечно) или
    если сравнение срока перевёрнуто (``expires_at`` проверяется наоборот): тогда
    живое надгробие сносится, а протухшее остаётся — то есть ровно наоборот.
    """
    for operation_id, expires_at in (
        ("op_expired", NOW - 1),
        ("op_live", NOW + 60 * 60),
    ):
        handle.write_json(
            ad.tombstone_marker(operation_id),
            {"operation_id": operation_id, "removed_at": NOW - 10, "expires_at": expires_at},
            subdir=ad.ARTIFACTS_SUBDIR,
        )

    ad._sweep(handle, now=NOW)

    assert not _tombstone_exists(handle, "op_expired")
    assert _tombstone_exists(handle, "op_live")
    assert ad.load_tombstone(handle, "op_expired", now=NOW) is None
    assert ad.load_tombstone(handle, "op_live", now=NOW) is not None


def test_tombstone_carries_fact_and_time_but_not_the_removed_work(
    handle: SessionHandle,
) -> None:
    """Надгробие переживает само состояние, а значит — и его содержимое.

    Краснеет, если в тело надгробия попадёт путь к документу, текст или сводка:
    надгробие живёт дольше удалённого состояния и отдаётся читателю, то есть
    убранный разбор стал бы читаться ещё неделю после того, как объявлен срок.
    """
    operation_id = _operation_id()
    _document(handle)
    _state(
        handle,
        operation_id=operation_id,
        status="requires_continuation",
        document_path=DOC_NAME,
        last_access_at=NOW - ad.INCOMPLETE_TTL_SEC - 60,
    )

    ad._sweep(handle, now=NOW)

    payload = _tombstone(handle, operation_id)
    assert payload["operation_id"] == operation_id
    assert payload["reason"] == "ttl_expired"
    assert payload["removed_at"] == NOW
    assert payload["expires_at"] == NOW + ad.TOMBSTONE_TTL_SEC
    assert set(payload) == {
        "operation_id",
        "removed_at",
        "removed_at_iso",
        "reason",
        "orphaned",
        "expires_at",
    }
    assert "Статья" not in json.dumps(payload, ensure_ascii=False)
    assert DOC_NAME not in json.dumps(payload, ensure_ascii=False)