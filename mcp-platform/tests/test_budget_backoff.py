"""Уменьшение ограничения шага после вызова, не уложившегося в потолок.

Что проверяется. ``_batch_budget`` отвечает на первую половину задачи 4.4 —
ограничение помещается в потолок вызова. Здесь закрыта вторая: при повторном
неукладывании ограничение уменьшается, и уменьшение **переживает вызов**.

Почему перехват протухшего признака занятости — это и есть наблюдаемое
неукладывание. Поток, не уложившийся в потолок, не прерывается
(``future.cancel()`` для идущей задачи не действует), и его возврат
отбрасывается целиком, то есть вызов не возвращает ничего: ни ответа, ни отказа,
ни записи состояния. Снаружи остаётся ровно один след — признак занятости,
который поток удерживает до своего ``finally``. Пока срок признака не истёк,
следующий вызов получает ``operation_in_progress`` вместо дублирования работы
(сценарий «Поток пережил таймаут»); по истечении срока он берёт работу вместо
прежнего владельца — и этот момент перехвата и есть сигнал.

Почему пробы идут через ``create_tool``, а не через внутренние функции.
Уменьшение проверяется **вторым вызовом**: запись на диске сама по себе ничего не
доказывает, доказывает её прочтение следующим вызовом. Поэтому каждая проба
делает минимум два вызова и сравнивает то, что домен реально получил.

Время в пробах — настоящее. Причина не в аккуратности: срок жизни признака
сравнивается с ``time.time()`` самой операцией, и подставленная эпоха была бы
не протухшей либо протухшей не по той причине.
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
from libs.enterprise_common.session.workspace import SessionWorkspace  # noqa: E402
from servers.enterprise.tools import analyze_document as ad  # noqa: E402

SESSION_ID = "sess-budget"
DOCUMENT = "Статья 1. Договор. Статья 2. Предмет. Статья 3. Цена."
DOCUMENT_NAME = "doc.txt"

#: Столько батчей просим у расчётного бюджета там, где надо отличить уменьшение
#: вдвое от нижней границы: одна единица уменьшения дала бы ноль, и проба
#: прошла бы по нижней границе, а не по делителю.
ROOMY_BUDGET = 6


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    """Папка сессий в ``tmp_path``: операция не должна писать наружу."""
    return SessionWorkspace(tmp_path / "sessions")


def _ctx(request_id: str = "req-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id=SESSION_ID, user_id="u-1"
        ),
        tool_name="platform.analyze_document",
        capability="platform",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class _FakeDomain:
    """Подмена ``service.run``, у которой в подписи есть ``batch_limit``.

    Подпись объявлена явно, а не ``**kwargs``: отбор ``_accepted_kwargs`` смотрит
    в подпись, а не в вызов, и подмена с ``**kwargs`` отбросила бы ограничение
    молча — проба позеленела бы вхолостую, не увидев ни одного аргумента.

    ``seen`` накапливает всё, что домен получил, поэтому проба может отличить
    «ограничение уменьшилось» от «домен не позван вовсе».
    """

    def __init__(self) -> None:
        self.seen: list[int | None] = []
        self.status = "requires_continuation"

    def run(
        self,
        text: str,
        *,
        length: str = "brief",
        focus: str | None = None,
        question: str | None = None,
        confirmed: bool = False,
        document_path: str | None = None,
        workspace_root: Path | str | None = None,
        batch_limit: int | None = None,
        call_budget_sec: float | None = None,
    ) -> dict[str, Any]:
        self.seen.append(batch_limit)
        return {"status": self.status, "summary": {"context_batches_total": 3}}


@pytest.fixture()
def domain(monkeypatch: pytest.MonkeyPatch) -> _FakeDomain:
    fake = _FakeDomain()
    monkeypatch.setattr(
        "libs.legal_summarizer.application.service.run", fake.run, raising=True
    )
    return fake


def _timeout_with_budget(batches: int) -> float:
    """Потолок вызова, при котором расчётный бюджет равен ровно ``batches``.

    Потолок подбирается по **объявленной доменом** оценке батча, а не берётся из
    настроек контура числом: иначе проба опиралась бы на частную настройку и молча
    разъехалась бы при её смене. Проверка самого расчёта внутри хелпера — условие
    пробы: если расчётный бюджет не тот, проба молча проверяла бы не то.
    """
    from libs.legal_summarizer.llm.config import get_execution_config

    per_batch = float(get_execution_config()["estimated_chunk_duration_sec"])
    assert per_batch > ad.BATCH_BUDGET_SHARE, "запас на округление вниз кончился"
    timeout = batches * per_batch / ad.BATCH_BUDGET_SHARE + 1.0
    assert ad._batch_budget(timeout) == batches, timeout
    return timeout


def _prepare(workspace: SessionWorkspace) -> Any:
    """Папка сессии с документом: разбор без вложения не начинается.

    Готовится на каждый вызов и в каждой пробе, а не по порядку: забытое
    вложение падало бы с ``document_not_found`` и выглядело бы как отказ
    проверяемого правила.
    """
    handle = workspace.handle(SESSION_ID, create=True)
    (handle.subdir(ad.FILES_SUBDIR) / DOCUMENT_NAME).write_text(
        DOCUMENT, encoding="utf-8"
    )
    return handle


def _operation_id(workspace: SessionWorkspace) -> str:
    """Идентификатор операции — тот же расчёт, что делает операция."""
    document = _prepare(workspace).subdir(ad.FILES_SUBDIR) / DOCUMENT_NAME
    return ad._compute_operation_id(
        DOCUMENT, "brief", document_path=str(document), question="", focus=""
    )


def _call(
    workspace: SessionWorkspace, timeout: float, *, request_id: str = "req-1"
) -> dict[str, Any]:
    """Один вызов разбора; возвращает разобранный ответ операции."""
    _prepare(workspace)
    definition = ad.create_tool(workspace, execution_timeout_sec=timeout)
    raw = definition.handler(
        _ctx(request_id),
        document=f"session://files/{DOCUMENT_NAME}",
        confirmed=True,
    )
    return json.loads(raw)


def _budget_path(workspace: SessionWorkspace, operation_id: str) -> Path:
    """Где физически лежит применённое ограничение шага."""
    handle = workspace.handle(SESSION_ID, create=True)
    return handle.subdir(ad.ARTIFACTS_SUBDIR) / ad.budget_marker(operation_id)


def _leave_marker(handle: Any, operation_id: str, *, expired: bool) -> None:
    """Оставить признак занятости, какой оставляет поток, переживший таймаут.

    Живой поток снимает признак в ``finally``, то есть **после** того, как вызов
    уже отдан конвейером по потолку. Пока срок не истёк, признак удерживается и
    следующий вызов честно отказывает; ``expired=True`` — тот же признак, но
    доживший до конца срока, то есть перехватываемый.
    """
    now = time.time()
    ad._write_marker(
        handle,
        ad.busy_marker(operation_id),
        {
            "operation_id": operation_id,
            "step": 4,
            "session_token": "req-overrun",
            "taken_at": now - 10.0,
            "expires_at": now - 1.0 if expired else now + 3600.0,
        },
    )


class TestBudgetIsStoredInOperationState:
    """Ограничение — часть состояния операции, а не отдельный файл рядом."""

    def test_budget_lands_inside_the_operation_state(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)

        body = _call(workspace, timeout)

        # Ответ операции и её собственный расчёт обязаны совпасть: иначе проба
        # правила смотрела бы на чужое состояние и видела бы чужой файл.
        assert body["operation_id"] == operation_id
        assert domain.seen == [ROOMY_BUDGET]

        state_dir = ad.operations_dir(workspace.handle(SESSION_ID, create=True)) / operation_id
        stored = _budget_path(workspace, operation_id)
        # Внутри каталога состояния, рядом с пошаговыми файлами чанков: уборка
        # сносит каталог целиком, и ограничение умирает вместе с разбором.
        assert stored.parent == state_dir
        assert stored.is_file()
        payload = json.loads(stored.read_text(encoding="utf-8"))
        assert payload["batch_budget"] == ROOMY_BUDGET

    def test_budget_dies_with_the_state(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Убранное состояние уносит ограничение с собой, а не оставляет его."""
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)
        stored = _budget_path(workspace, operation_id)
        assert stored.is_file()

        handle = workspace.handle(SESSION_ID, create=True)
        ad._write_marker(
            handle,
            ad.access_marker(operation_id),
            {
                "operation_id": operation_id,
                # Возраст состояния считается уборкой по отметке обращения;
                # выставленный срок протухшее состояние объясняет надгробием.
                "last_access_at": time.time() - ad.INCOMPLETE_TTL_SEC - 10,
                "status": "requires_continuation",
            },
        )
        assert ad._sweep(handle, now=time.time())["removed"] == 1

        assert not stored.exists()
        assert domain.seen == [ROOMY_BUDGET]

    def test_corrupt_record_falls_back_to_the_ceiling(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Негодная запись означает «считать заново», а не отказ вызова."""
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)
        stored = _budget_path(workspace, operation_id)
        stored.write_text("{ битый json", encoding="utf-8")

        _call(workspace, timeout, request_id="req-2")

        assert domain.seen == [ROOMY_BUDGET, ROOMY_BUDGET]


class TestTakeoverHalvesTheBudget:
    """Перехват протухшего признака — единственный наблюдаемый недобор."""

    def test_first_call_gets_the_calculated_ceiling(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        timeout = _timeout_with_budget(ROOMY_BUDGET)

        _call(workspace, timeout)
        # Второй вызов без всякого перехвата обязан получить то же: уменьшать
        # без повода нельзя, иначе ограничение падало бы само с собой.
        _call(workspace, timeout, request_id="req-2")

        assert domain.seen == [ROOMY_BUDGET, ROOMY_BUDGET]

    def test_takeover_halves_the_budget(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)

        _leave_marker(workspace.handle(SESSION_ID, create=True), operation_id, expired=True)
        _call(workspace, timeout, request_id="req-2")

        assert domain.seen == [ROOMY_BUDGET, ROOMY_BUDGET // 2]

    def test_halving_stops_at_one(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Единица — пол: дальше уменьшать нечего, и ноль домен прочитал бы
        как «шаг не поместился», то есть как другой отказ с другой причиной."""
        timeout = _timeout_with_budget(1)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)

        _leave_marker(workspace.handle(SESSION_ID, create=True), operation_id, expired=True)
        _call(workspace, timeout, request_id="req-2")

        assert domain.seen == [1, 1]
        assert ad.MIN_BATCH_BUDGET == 1

    def test_live_marker_refuses_and_touches_nothing(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Пока срок признака не истёк, вызов отказывает — и бюджета не касается.

        Это отличает «признак занятости есть» от «признак протух»: уменьшать по
        живому признаку нельзя, поток идущей работы никуда не делся.
        """
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)
        handle = workspace.handle(SESSION_ID, create=True)
        _leave_marker(handle, operation_id, expired=False)

        with pytest.raises(EnterpriseError) as excinfo:
            _call(workspace, timeout, request_id="req-2")
        assert excinfo.value.code == "operation_in_progress"
        # Домен не позван: отказ «занято» стоит ноль LLM-вызовов.
        assert domain.seen == [ROOMY_BUDGET]

        # Поток дошёл до конца и снял признак — следующий вызов идёт с прежним
        # ограничением, а не с уменьшенным.
        ad._release(handle, operation_id)
        _call(workspace, timeout, request_id="req-3")
        assert domain.seen == [ROOMY_BUDGET, ROOMY_BUDGET]


class TestBudgetDoesNotComeBackOnItsOwn:
    """Уменьшенное ограничение остаётся уменьшенным, пока разбор не закончен."""

    def test_repeated_calls_keep_the_reduced_budget(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)
        _leave_marker(workspace.handle(SESSION_ID, create=True), operation_id, expired=True)

        _call(workspace, timeout, request_id="req-2")
        # Дальше перехвата нет: признак снят в ``finally`` прошлого вызова, то
        # есть механизм не срабатывает сам по себе. Уменьшенное значение держится
        # — и не вырастает обратно, и не уменьшается ещё раз.
        _call(workspace, timeout, request_id="req-3")
        _call(workspace, timeout, request_id="req-4")

        assert domain.seen == [
            ROOMY_BUDGET,
            ROOMY_BUDGET // 2,
            ROOMY_BUDGET // 2,
            ROOMY_BUDGET // 2,
        ]

    def test_second_takeover_halves_again(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Повторный недобор уводит ограничение ещё вдвое — до пола."""
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        handle = workspace.handle(SESSION_ID, create=True)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)

        _leave_marker(handle, operation_id, expired=True)
        _call(workspace, timeout, request_id="req-2")
        _leave_marker(handle, operation_id, expired=True)
        _call(workspace, timeout, request_id="req-3")

        assert domain.seen == [ROOMY_BUDGET, 3, 1]

    def test_completed_resets_the_budget(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Законченный разбор закрывает эпоху ограничения: дальше — с потолка."""
        timeout = _timeout_with_budget(ROOMY_BUDGET)
        operation_id = _operation_id(workspace)
        _call(workspace, timeout)
        _leave_marker(workspace.handle(SESSION_ID, create=True), operation_id, expired=True)
        _call(workspace, timeout, request_id="req-2")

        domain.status = "completed"
        _call(workspace, timeout, request_id="req-3")

        payload = json.loads(
            _budget_path(workspace, operation_id).read_text(encoding="utf-8")
        )
        assert payload["batch_budget"] is None

        domain.status = "requires_continuation"
        _call(workspace, timeout, request_id="req-4")
        assert domain.seen == [ROOMY_BUDGET, 3, 3, ROOMY_BUDGET]

    def test_budget_never_exceeds_a_shrunk_ceiling(
        self, workspace: SessionWorkspace, domain: _FakeDomain
    ) -> None:
        """Записанное ограничение не выше потолка, под которым оно применяется.

        Потолок приходит из конфигурации, а значит может уменьшиться между
        вызовами; ограничение, перешагнувшее потолок, защищало бы там, где
        защиты нет.
        """
        roomy = _timeout_with_budget(ROOMY_BUDGET)
        tight = _timeout_with_budget(2)
        _call(workspace, roomy)

        _call(workspace, tight, request_id="req-2")

        assert domain.seen == [ROOMY_BUDGET, 2]