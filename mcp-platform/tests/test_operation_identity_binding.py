"""Личность оборота доходит до LLM-вызова домена.

Дефект, который держит страж. Операция ``platform.analyze_document``
выполняет домен **в том же процессе**, и домен ходит в LLM отдельным клиентом,
который берёт личность сам — из ``llm.config.get_identity()`` — и уходит с ней
как ``params._meta``. Ни операция, ни capability личность не привязывали:
``configure()`` на границе capability задаёт только ``cache_root``.

Следствие было тихим и полным: каждый LLM-вызов отклонялся кодом
``identity_missing``, ``execution/map_reduce.py`` глотал исключение в
``final_summary = ""``, домен отдавал ``REDUCE_INPUT_EMPTY``, и **любой**
настоящий документ заканчивался отказом. Юнит-пробы этого не видели: они
подменяют домен, а подмена в LLM не ходит, то есть отказ возникает только на
живом вызове.

Проба идёт через настоящую операцию и смотрит, что домен **видел в момент своей
работы**, а не что куда-то записалось: привязка контекстная, и запись «личность
есть в файле» доказала бы не то же самое.

Невакуумность обязательна и проверяется подсаженным дефектом: снятая привязка
обязана давать красное.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext,
    ToolExecutionContext,
)
from libs.enterprise_common.session.workspace import SessionWorkspace  # noqa: E402
from libs.legal_summarizer.llm import config as domain_config  # noqa: E402
from servers.enterprise.tools import analyze_document as ad  # noqa: E402

SESSION_ID = "sess-identity-binding"
DOCUMENT = "Статья 1. Предмет. Статья 2. Цена. Статья 3. Расчёты."
DOCUMENT_NAME = "doc.txt"

#: Ключи, которые домен читает сам (``llm/client.py::_identity``) — **простые
#: имена**. Пространственные ``workspaces/*`` тут не подошли бы: ридер ищет
#: ``session_id`` и ``user_id`` без префикса, и ``as_meta()`` молча не нашлось бы.
EXPECTED_IDENTITY = {
    "session_id": SESSION_ID,
    "user_id": "u-binding",
    "request_id": "req-binding",
}


def _domain_identity_reader() -> Any:
    """Ридер личности, каким домен пользуется в бою.

    Импортируется лениво и поимённо: страж зовёт **тот же** код, что и рабочий
    путь, а не свою копию правил. Смена контракта в домене роняет страж, а не
    оставляет его зелёным на старой форме.
    """
    from libs.legal_summarizer.llm.client import _identity

    return _identity()


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


class _IdentityProbe:
    """Домен, который записывает личность, виден ему в момент работы.

    Ключи проба **не выбирает сама**: личность читается тем же ридером, которым
    домен пользуется в бою, — ``llm/client.py::_identity``. Собственная сборка
    ключей в пробе проверяла бы согласие пробы с собой же: первый вариант стержа
    ждал ``workspaces/*`` (как отдаёт ``as_meta``), домен ждал простые имена, и
    страж был зелёным при неработающем разборе.

    Подпись объявлена явно, а не ``**kwargs``: отбор ``_accepted_kwargs``
    смотрит в подпись, и подмена с ``**kwargs`` отбросила бы аргументы молча —
    проба позеленела бы вхолостую.
    """

    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []
        self.as_context: list[Any] = []

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
        self.seen.append(domain_config.get_identity())
        self.as_context.append(_domain_identity_reader())
        return {"status": "requires_continuation", "summary": {"context_batches_total": 2}}


@pytest.fixture()
def probe(monkeypatch: pytest.MonkeyPatch) -> _IdentityProbe:
    fake = _IdentityProbe()
    monkeypatch.setattr(
        "libs.legal_summarizer.application.service.run", fake.run, raising=True
    )
    return fake


def _ctx(request_id: str = "req-binding") -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id=SESSION_ID, user_id="u-binding"
        ),
        tool_name="platform.analyze_document",
        capability="platform",
        started_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
    )


def _call(workspace: SessionWorkspace) -> dict[str, Any]:
    handle = workspace.handle(SESSION_ID, create=True)
    (handle.subdir(ad.FILES_SUBDIR) / DOCUMENT_NAME).write_text(DOCUMENT, encoding="utf-8")
    definition = ad.create_tool(workspace, execution_timeout_sec=120.0)
    raw = definition.handler(
        _ctx(),
        document=f"session://files/{DOCUMENT_NAME}",
        confirmed=True,
    )
    return json.loads(raw)


class TestDomainSeesTheCallIdentity:
    def test_domain_sees_exactly_the_call_identity(
        self, workspace: SessionWorkspace, probe: _IdentityProbe
    ) -> None:
        """Домен во время своей работы видит личность вызова — все три ключа.

        Краснеет, если привязку снять: домен увидит пустой словарь, и это
        ровно то состояние, при котором LLM-вызов отклоняется кодом
        ``identity_missing``, а разбор документа заканчивается отказом.
        """
        body = _call(workspace)

        assert body["status"] == "requires_continuation"
        assert len(probe.seen) == 1, f"домен позван {len(probe.seen)} раз(а) — проба смотрит не туда"
        assert probe.seen[0] == EXPECTED_IDENTITY, (
            f"домен увидел личность {probe.seen[0]!r}, а обязан был {EXPECTED_IDENTITY!r}"
        )

    def test_боевой_ридер_домена_собирает_контекст(
        self, workspace: SessionWorkspace, probe: _IdentityProbe
    ) -> None:
        """Ридер домена собирает из привязанного полноценный контекст вызова.

        Это проверка настоящего контракта, а не моей подстановки. Первый вариант
        стража ждал ``workspaces/*``, домен читал простые имена, проба была
        зелёной — а разбор на живом файле падал с ``REDUCE_INPUT_EMPTY``.
        Именно этот разрыв и ловится здесь: собирается ``None`` вместо контекста
        значит, что на провод уйдёт вызов без ``_meta`` и будет отклонён кодом
        ``identity_missing``.
        """
        _call(workspace)

        context = probe.as_context[0]
        assert context is not None, (
            "ридер домена не собрал контекст вызова из привязанной личности: "
            f"привязка была {probe.seen[0]!r}, а домен читает простые имена "
            "session_id/user_id без префикса"
        )
        assert context.session_id == SESSION_ID
        assert context.user_id == "u-binding"

    def test_identity_is_released_after_the_call(
        self, workspace: SessionWorkspace, probe: _IdentityProbe
    ) -> None:
        """Личность не остаётся висеть после вызова.

        Утечка опаснее отсутствия: следующий оборот, даже чужой, пошёл бы в
        журнал под чужой сессией, и журнал разбирать было бы уже нечем.
        """
        _call(workspace)

        assert domain_config.get_identity() == {}, (
            "после вызова личность осталась в контексте: "
            f"{domain_config.get_identity()!r}"
        )

    def test_two_calls_in_a_row_do_not_leak_into_each_other(
        self, workspace: SessionWorkspace, probe: _IdentityProbe
    ) -> None:
        """Два вызова подряд не делят личность.

        Смешение проявилось бы на втором: он увидел бы ``request_id`` первого,
        и корреляция разбора в журнале указала бы на чужой вызов.

        Документы разные намеренно: идентификатор операции считается из
        содержимого, поэтому два вызова одного документа столкнулись бы за
        признак занятости и второй был бы отвергнут — то есть проба проверяла
        бы отказ занятости вместо подмены личности.
        """
        second = "Статья 4. Ответственность. Статья 5. Споры."

        for name, text, rid in (
            (DOCUMENT_NAME, DOCUMENT, "req-first"),
            ("doc2.txt", second, "req-second"),
        ):
            handle = workspace.handle(SESSION_ID, create=True)
            (handle.subdir(ad.FILES_SUBDIR) / name).write_text(text, encoding="utf-8")
            definition = ad.create_tool(workspace, execution_timeout_sec=120.0)
            definition.handler(
                _ctx(rid),
                document=f"session://files/{name}",
                confirmed=True,
            )

        assert len(probe.seen) == 2, f"домен позван {len(probe.seen)} раз(а) вместо двух"
        assert [meta["request_id"] for meta in probe.seen] == [
            "req-first",
            "req-second",
        ], f"порядок или подмена личности: {probe.seen}"


def test_binding_keeps_declared_settings(
    workspace: SessionWorkspace, probe: _IdentityProbe
) -> None:
    """Привязка личности не затирает настройки, объявленные на процесс.

    Настройки объявляются один раз на весь сервер, личность — на каждый вызов.
    Если бы привязка начиналась с пустой конфигурации, объявленный владельцем
    корень кэша или длина по умолчанию молча возвращались бы к дефолтам на
    каждом вызове, и порок выглядел бы как «домен сбросил настройки».
    """
    declared = domain_config.with_overrides(cache_root="X:/declared-root")
    domain_config.configure(declared)

    _call(workspace)

    assert domain_config.get_cache_root() == "X:/declared-root", (
        "объявленный корень потерян внутри привязки личности"
    )