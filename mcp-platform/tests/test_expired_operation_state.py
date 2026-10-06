"""Протухшая ссылка на состояние операции: надгробие уборки и объяснение.

Что ломалось. Уборка (``_sweep``) удаляла состояние молча, и читатель
(``query_operation``) различал «состояние никогда не было» и «состояние было и
убрано по сроку» только по догадке модели. Домен на протухшую ссылку отвечает
``manifest_not_found``, и его текст перечисляет **обе** причины сразу —
«убрано по истечении срока жизни **либо** operation_id указан неверно», — то
есть не называет ни одну. Модель обязана была выбрать сама и выбирала наугад.

Здесь закрывается ровно эта разница:

* уборка оставляет **надгробие** — факт и время, без удалённых данных;
* читатель при наличии надгробия отвечает отказом, который называет срок и
  предлагает начать заново, и не оставляет «либо»;
* без надгробия отказ прежний, с прежним перечислением причин;
* надгробие имеет свой срок, не продлевает жизнь состояния и не мешает
  уборке следующих.

Время в пробах — настоящее, а не выдуманная эпоха. Причина не в аккуратности:
читатель берёт ``time.time()`` сам, поэтому надгробие, записанное уборкой на
чужой шкале, к моменту чтения было бы протухшим и объяснение не сработало бы.
Уборка и чтение обязаны идти по одной шкале.

Вторая часть файла — про ``call_budget_sec``: потолок вызова передаётся домену
по имени, и проверяется это подменённой функцией, **подпись которой содержит
нужный параметр**. Подмена с ``**kwargs`` здесь бесполезна: отбор
``_accepted_kwargs`` смотрит в подпись, а не в вызов, и имя, не объявленное в
подписи, отбрасывается молча — проба позеленела бы вхолостую.
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
from servers.enterprise.tools.query_operation import create_tool  # noqa: E402

#: Запасной корень намеренно не задан: вызовы с сессией обязаны работать без
#: объявления владельца, иначе проба зависела бы от посторонней настройки.
SESSION_ID = "sess-expired"

#: Домен перечисляет обе причины отказа, не называя ни одной. Наличие этого
#: «либо» — признак прежнего отказа: без него читатель знает причину.
_DOMAIN_HEDGES = "либо operation_id указан неверно"


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


@pytest.fixture()
def handle(workspace: SessionWorkspace) -> Any:
    return workspace.handle(SESSION_ID, create=True)


@pytest.fixture()
def reader(workspace: SessionWorkspace) -> Any:
    return create_tool(workspace, fallback_cache_root=None).handler


def _ctx(session_id: str = SESSION_ID) -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id="req-expired", session_id=session_id, user_id="u-1"
        ),
        tool_name="platform.query_operation",
        capability="platform",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _write_state(handle: Any, operation_id: str, *, status: str = "completed") -> Path:
    """Состояние операции: каталог и манифест, как их оставляет домен."""
    target = ad.operations_dir(handle) / operation_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "operation_id": operation_id,
                "status": status,
                "length": "brief",
                "document_path": "doc.txt",
            }
        ),
        encoding="utf-8",
    )
    return target


def _age(handle: Any, operation_id: str, *, last_access: float, status: str) -> None:
    """Отметка обращения: по ней уборка считает возраст состояния."""
    ad._write_marker(
        handle,
        ad.access_marker(operation_id),
        {
            "operation_id": operation_id,
            "last_access_at": last_access,
            "status": status,
        },
    )


def _expired(handle: Any, operation_id: str, *, status: str = "completed") -> float:
    """Подготовить состояние к уборке и убрать его; вернуть момент уборки.

    Возраст и срок берутся по одному статусу, иначе состояние, помеченное
    ``completed``, ждало бы недели, а заготовка думала бы, что у него часы.
    """
    now = time.time()
    ttl = ad.COMPLETE_TTL_SEC if status == "completed" else ad.INCOMPLETE_TTL_SEC
    _write_state(handle, operation_id, status=status)
    _age(handle, operation_id, last_access=now - ttl - 10, status=status)
    assert ad._sweep(handle, now=now)["removed"] == 1
    return now


def _tombstone_path(handle: Any, operation_id: str) -> Path:
    return (
        handle.subdir(ad.ARTIFACTS_SUBDIR)
        / ad.SKILL_DIRNAME
        / ad.TOMBSTONES_DIRNAME
        / f"{operation_id}.json"
    )


# ── уборка оставляет надгробие ────────────────────────────────────────────


class TestSweepWritesTombstone:
    def test_expired_state_leaves_tombstone(self, handle: Any) -> None:
        """Протухшее состояние удалено — надгробие на его месте есть."""
        _expired(handle, "op-1")

        assert _tombstone_path(handle, "op-1").is_file()

    def test_tombstone_states_why_and_when(self, handle: Any) -> None:
        """В надгробии — факт уборки и время, а не молчание."""
        now = time.time()
        _write_state(handle, "op-2")
        _age(handle, "op-2", last_access=now - ad.COMPLETE_TTL_SEC - 10, status="completed")

        ad._sweep(handle, now=now)

        payload = json.loads(_tombstone_path(handle, "op-2").read_text(encoding="utf-8"))
        assert payload["reason"] == "ttl_expired"
        assert payload["removed_at"] == now
        assert payload["operation_id"] == "op-2"
        # Время обязано быть читаемым человеком, а не только машиной: ответ
        # читает модель, а не журнал.
        assert payload["removed_at_iso"].startswith(time.strftime("%Y"))

    def test_fresh_state_leaves_no_tombstone(self, handle: Any) -> None:
        """Живое состояние не убирается и надгробия не получает.

        Обратная сторона: надгробие о живом состоянии объявляло бы протухание
        там, где работа продолжается.
        """
        now = time.time()
        _write_state(handle, "op-live")
        _age(handle, "op-live", last_access=now, status="completed")

        assert ad._sweep(handle, now=now)["removed"] == 0
        assert not _tombstone_path(handle, "op-live").exists()
        assert (ad.operations_dir(handle) / "op-live").is_dir()

    def test_tombstone_carries_no_removed_data(self, handle: Any) -> None:
        """Надгробие не возвращает удалённое: ни текста, ни пути документа.

        Надгробие переживает само состояние, поэтому содержимое состояния в нём
        пережило бы тоже — а ответ на протухшую ссылку не должен нести ни куска
        разобранного документа.
        """
        now = time.time()
        target = _write_state(handle, "op-secret")
        (target / "result.json").write_text("СЕКРЕТНЫЙ-ТЕКСТ-ДОКУМЕНТА", encoding="utf-8")
        _age(handle, "op-secret", last_access=now - ad.COMPLETE_TTL_SEC - 10, status="completed")

        ad._sweep(handle, now=now)

        raw = _tombstone_path(handle, "op-secret").read_text(encoding="utf-8")
        assert "СЕКРЕТНЫЙ-ТЕКСТ-ДОКУМЕНТА" not in raw
        assert "doc.txt" not in raw
        # Разрешено ровно то, что нужно для объяснения: что, когда и почему.
        assert set(json.loads(raw)) == {
            "operation_id",
            "removed_at",
            "removed_at_iso",
            "reason",
            "orphaned",
            "expires_at",
        }


# ── читатель отвечает на протухшую ссылку ─────────────────────────────────


class TestReaderAnswersExpiredState:
    def test_expired_state_names_ttl_and_offers_restart(self, reader: Any, handle: Any) -> None:
        """Отказ называет срок жизни и предлагает начать разбор заново."""
        _expired(handle, "op-3")

        with pytest.raises(EnterpriseError) as raised:
            reader(_ctx(), operation_id="op-3", field="stats")

        error = raised.value
        assert error.code == "not_found"
        text = error.message
        assert "срока жизни" in text.lower()
        assert "заново" in text.lower()
        # Причина названа, а не перечислена: «либо» здесь означало бы, что
        # читатель знает причину не лучше прежнего.
        assert _DOMAIN_HEDGES not in text

    def test_expired_state_answers_with_its_removal_time(self, reader: Any, handle: Any) -> None:
        """Отказ называет, когда состояние убрано.

        «Убрано когда-то» не отличает потерю минутной давности от потери
        месячной, и модель не может решить, стоит ли продолжать этот разбор.
        """
        _expired(handle, "op-4")

        with pytest.raises(EnterpriseError) as raised:
            reader(_ctx(), operation_id="op-4", field="stats")

        assert time.strftime("%Y") in raised.value.message

    def test_never_existed_state_refuses_without_explanation(self, reader: Any) -> None:
        """Без надгробия отказ прежний, с прежним перечислением причин.

        Состояния не было никогда — объяснять нечего, а выдуманное «убрано по
        сроку» направило бы модель повторять заведомо несуществующий разбор
        вместо того, чтобы сверить operation_id.
        """
        with pytest.raises(EnterpriseError) as raised:
            reader(_ctx(), operation_id="op-never", field="stats")

        error = raised.value
        assert error.code == "not_found"
        assert _DOMAIN_HEDGES in error.message
        assert "2023-" not in error.message

    def test_live_state_is_not_declared_expired(self, reader: Any, handle: Any) -> None:
        """Живое состояние читается, а не отказывает как протухшее."""
        now = time.time()
        _write_state(handle, "op-live2", status="requires_continuation")
        _age(handle, "op-live2", last_access=now, status="requires_continuation")

        payload = json.loads(reader(_ctx(), operation_id="op-live2", field="stats"))

        assert payload["operation_id"] == "op-live2"

    def test_stale_tombstone_next_to_live_state_is_ignored(self, reader: Any, handle: Any) -> None:
        """Устаревшее надгробие при живом состоянии не отказывает в чтении.

        Такое возможно: состояние пересобрали с тем же идентификатором после
        уборки. Отказ по старому надгробию запретил бы продолжать работу,
        которая идёт прямо сейчас.
        """
        now = time.time()
        _write_state(handle, "op-rebuilt", status="requires_continuation")
        ad._write_marker(
            handle,
            ad.tombstone_marker("op-rebuilt"),
            {
                "operation_id": "op-rebuilt",
                "removed_at": now,
                "reason": "ttl_expired",
                "expires_at": now + ad.TOMBSTONE_TTL_SEC,
            },
        )

        payload = json.loads(reader(_ctx(), operation_id="op-rebuilt", field="stats"))

        assert payload["operation_id"] == "op-rebuilt"


# ── срок жизни надгробия ──────────────────────────────────────────────────


class TestTombstoneLifetime:
    def test_tombstone_ttl_not_shorter_than_state_ttl(self) -> None:
        """Срок надгробия не меньше срока самого состояния.

        Иначе ссылка, выданная ещё живым состоянием, привела бы к отказу без
        объяснения — ровно то, что надгробие и чинит.
        """
        assert ad.TOMBSTONE_TTL_SEC >= ad.COMPLETE_TTL_SEC
        assert ad.TOMBSTONE_TTL_SEC >= ad.INCOMPLETE_TTL_SEC

    def _tombstone(self, handle: Any, operation_id: str, *, removed_at: float) -> None:
        ad._write_marker(
            handle,
            ad.tombstone_marker(operation_id),
            {
                "operation_id": operation_id,
                "removed_at": removed_at,
                "removed_at_iso": datetime.fromtimestamp(
                    removed_at, tz=timezone.utc
                ).isoformat(),
                "reason": "ttl_expired",
                "orphaned": False,
                "expires_at": removed_at + ad.TOMBSTONE_TTL_SEC,
            },
        )

    def test_expired_tombstone_is_purged_by_sweep(self, handle: Any) -> None:
        """Протухшее надгробие снимается уборкой: каталог не растёт вечно."""
        old = time.time() - ad.TOMBSTONE_TTL_SEC - 60
        self._tombstone(handle, "op-old", removed_at=old)

        ad._sweep(handle, now=time.time())

        assert not _tombstone_path(handle, "op-old").exists()

    def test_fresh_tombstone_survives_sweep(self, handle: Any) -> None:
        """Живое надгробие уборка не снимает — объяснение ещё нужно."""
        self._tombstone(handle, "op-fresh", removed_at=time.time())

        ad._sweep(handle, now=time.time())

        assert _tombstone_path(handle, "op-fresh").is_file()

    def test_expired_tombstone_reads_as_absent(self, handle: Any) -> None:
        """Протухшее надгробие читается как его отсутствие."""
        old = time.time()
        self._tombstone(handle, "op-stale", removed_at=old)

        assert ad.load_tombstone(handle, "op-stale", now=old + 10) is not None
        assert (
            ad.load_tombstone(handle, "op-stale", now=old + ad.TOMBSTONE_TTL_SEC + 1)
            is None
        )

    def test_tombstone_does_not_extend_state_life(self, handle: Any) -> None:
        """Надгробие не продлевает жизнь состояния и не мешает следующей уборке.

        Надгробие лежит рядом с ``busy/`` и ``access/``, а не внутри
        ``operations/``: будь оно внутри, перебор уборки принял бы его за
        состояние и прибавил бы ему возраст заново с нуля.
        """
        assert ad.TOMBSTONES_DIRNAME != "operations"
        assert ad.tombstone_marker("op-x").startswith(
            f"{ad.SKILL_DIRNAME}/{ad.TOMBSTONES_DIRNAME}/"
        )

        now = time.time()
        _write_state(handle, "op-a")
        _age(
            handle,
            "op-a",
            last_access=now - ad.COMPLETE_TTL_SEC - 10,
            status="completed",
        )
        _write_state(handle, "op-b", status="requires_continuation")
        _age(
            handle,
            "op-b",
            last_access=now - ad.INCOMPLETE_TTL_SEC - 10,
            status="requires_continuation",
        )

        # Оба состояния убираются одним проходом, и каждое оставляет своё
        # надгробие: первое не помешало уборке второго.
        assert ad._sweep(handle, now=now)["removed"] == 2
        assert _tombstone_path(handle, "op-a").is_file()
        assert _tombstone_path(handle, "op-b").is_file()


# ── потолок вызова доходит до отбора ──────────────────────────────────────


class TestCallBudgetWiring:
    def test_operation_offers_call_budget_to_domain_signature(
        self, monkeypatch: pytest.MonkeyPatch, workspace: SessionWorkspace
    ) -> None:
        """Операция предлагает ``call_budget_sec`` функции, у которой он есть.

        Подменённая функция объявляет параметр в подписи **явно**. Пока
        параллельная правка ``service.py`` не приземлена, настоящая
        ``service.run`` отбросила бы значение молча, и проба позеленела бы
        вхолостую; подмена с ``**kwargs`` отбросила бы его по той же причине —
        отбор смотрит в подпись, а не в вызов. Проверяется ровно то, что
        утверждает операция: «значение предлагается».
        """
        seen: dict[str, Any] = {}

        def domain_run(
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
            seen.update(
                length=length,
                focus=focus,
                question=question,
                confirmed=confirmed,
                document_path=document_path,
                workspace_root=workspace_root,
                batch_limit=batch_limit,
                call_budget_sec=call_budget_sec,
            )
            return {"status": "completed", "operation_id": "op-x"}

        monkeypatch.setattr(
            "libs.legal_summarizer.application.service.run", domain_run, raising=True
        )

        handle = workspace.handle(SESSION_ID, create=True)
        document = handle.subdir(ad.FILES_SUBDIR) / "doc.txt"
        document.write_text("текст документа для разбора", encoding="utf-8")

        definition = ad.create_tool(workspace, execution_timeout_sec=45.0)
        definition.handler(
            _ctx(),
            document="doc.txt",
            length="brief",
            load_mode="brief",
            confirmed=True,
        )

        assert seen["call_budget_sec"] == 45.0
        # Тот же путь доставки, что и у соседних имён: потолок не должен
        # доходить отдельным вызовом, минуя отбор по подписи.
        assert seen["batch_limit"] == ad._batch_budget(45.0)

    def test_call_budget_passes_the_same_gate_as_batch_limit(self) -> None:
        """``call_budget_sec`` проходит тот же отбор, что и ``batch_limit``."""

        def signature(
            text: str,
            *,
            batch_limit: int | None = None,
            call_budget_sec: float | None = None,
        ) -> None:
            """Подпись домена: оба параметра объявлены явно."""

        accepted = ad._accepted_kwargs(
            signature, batch_limit=3, call_budget_sec=45.0, not_a_parameter=1
        )

        assert accepted == {"batch_limit": 3, "call_budget_sec": 45.0}

    def test_value_is_dropped_when_signature_lacks_the_parameter(self) -> None:
        """Подписи без параметра значение отбрасывает, а не роняет вызов.

        Пока ``call_budget_sec`` не приземлён в ``service.run``, настоящий
        домен ведёт себя именно так: вызов не падает. Отбор по подписи —
        общий для обоих имён, и это делает подключение безопасным независимо
        от того, приземлена ли параллельная правка.
        """

        def signature(text: str, *, batch_limit: int | None = None) -> None:
            """Подпись домена без потолка вызова."""

        accepted = ad._accepted_kwargs(signature, batch_limit=3, call_budget_sec=45.0)

        assert accepted == {"batch_limit": 3}