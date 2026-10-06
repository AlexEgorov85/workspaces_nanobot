"""Режим извлечения текста выводится из ``length``, а не выбирается моделью.

Дефект, который держит страж. У операции ``platform.analyze_document`` стоял
обязательный параметр ``load_mode`` — второй способ задать то же самое, что уже
задаёт ``length``. Он разошёлся с контрактом релиза 2.5.3, где аргумента загрузки
не было вовсе: CLI считал режим сам, ``load_mode = "brief" if length == "brief"
else "full"`` (``libs/legal_summarizer/cli.py:319``).

Чем это было хуже молчаливого выбора, который требование «Перевод вложения в
текст принадлежит операции» запрещало:

* перечень допустимых значений был ``("brief", "full")``, а объявленное в
  ``workspace/TOOLS.md`` значение — ``detailed``. Вызов, сделанный **точно по
  инструкции**, отвергался кодом ``invalid_params``;
* параметр был обязателен, то есть модель обязана была выбрать режим извлечения
  для режима свода, который выбирает она же и который к извлечению отношения не
  имеет;
* противоречащаяся пара ``length="brief"``, ``load_mode="full"`` проходила и
  разбирала документ целиком, объявляя краткую сводку, — то есть ровно то
  молчаливое расхождение, ради которого параметр и требовали.

Проба смотрит на шов, где решение обязано быть: ``document_io.load_text``, то
есть загрузчик домена. Подменить его — значит увидеть режим **на входе в
извлечение**, а не условный оператор в теле операции.

Схема проверяется отдельно, и проверка невакуумна: она требует, чтобы схема
была непустой и содержала ``length``. Иначе утверждение «параметра нет» прошло
бы на схеме, упавшей до пустоты.

Невакуумность обязательна и проверяется подсаженным дефектом: жёстко заданный
режим вместо выведенного обязан давать красное.
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

from libs.enterprise_common.errors import InvalidRequestError  # noqa: E402
from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext,
    ToolExecutionContext,
)
from libs.enterprise_common.session.workspace import SessionWorkspace  # noqa: E402
from servers.enterprise.tools import analyze_document as ad  # noqa: E402

DOCUMENT = "Статья 1. Предмет. Статья 2. Цена. Статья 3. Расчёты."
DOCUMENT_NAME = "doc.txt"


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


class _ExtractionProbe:
    """Загрузчик домена, который записывает полученный режим.

    Подпись объявлена явно, а не ``**kwargs``: ``_load_document_text`` зовёт
    ``load_text(path, mode=...)`` по имени, и подмена с ``**kwargs`` приняла бы
    любой вызов, не записав режим, — проба позеленела бы вхолостую.
    """

    def __init__(self) -> None:
        self.modes: list[str] = []

    def load_text(self, path: Path, mode: str = "full") -> str:
        self.modes.append(mode)
        return DOCUMENT


class _DomainProbe:
    """Домен, который отвечает без платной работы."""

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
        return {"status": "requires_continuation", "summary": {"context_batches_total": 1}}


@pytest.fixture()
def extraction(monkeypatch: pytest.MonkeyPatch) -> _ExtractionProbe:
    probe = _ExtractionProbe()
    monkeypatch.setattr(
        "libs.legal_summarizer.application.document_io.load_text",
        probe.load_text,
        raising=True,
    )
    monkeypatch.setattr(
        "libs.legal_summarizer.application.service.run",
        _DomainProbe().run,
        raising=True,
    )
    return probe


def _ctx(session_id: str = "sess-load-mode") -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id="req-load-mode", session_id=session_id, user_id="u-load-mode"
        ),
        tool_name="platform.analyze_document",
        capability="platform",
        started_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
    )


def _analyze(workspace: SessionWorkspace, session_id: str, **arguments: Any) -> str:
    handle = workspace.handle(session_id, create=True)
    (handle.subdir(ad.FILES_SUBDIR) / DOCUMENT_NAME).write_text(DOCUMENT, encoding="utf-8")
    definition = ad.create_tool(workspace, execution_timeout_sec=120.0)
    return definition.handler(
        _ctx(session_id),
        document=f"session://files/{DOCUMENT_NAME}",
        confirmed=True,
        **arguments,
    )


class TestExtractionModeComesFromLength:
    """Режим извлечения — следствие выбора свода, а не второго выбора."""

    @pytest.mark.parametrize(
        ("length", "expected_mode"),
        [("brief", "brief"), ("detailed", "full")],
    )
    def test_mode_is_derived_from_length(
        self,
        workspace: SessionWorkspace,
        extraction: _ExtractionProbe,
        length: str,
        expected_mode: str,
    ) -> None:
        _analyze(workspace, f"sess-{length}", length=length)

        assert extraction.modes == [expected_mode], (
            f"length={length!r} дал режим извлечения {extraction.modes!r}, "
            f"а ждали [{expected_mode!r}]"
        )

    def test_default_length_extracts_briefly(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        """Без ``length`` режим не выбирается заново — он выводится из дефолта
        ``brief``, то есть вызывающий не может получить свод от другого текста."""
        _analyze(workspace, "sess-default")

        assert extraction.modes == ["brief"], extraction.modes


class TestModeIsNotAModelArgument:
    """Поверхность модели — один параметр, а не два."""

    def test_published_schema_has_no_load_mode(self, workspace: SessionWorkspace) -> None:
        properties = ad.create_tool(workspace, execution_timeout_sec=120.0).input_schema[
            "properties"
        ]

        # Невакуумность: пустая схема прошла бы «параметра нет».
        assert properties, "схема операции пуста — проверка невакуумна лишь формально"
        assert "length" in properties, sorted(properties)
        assert "load_mode" not in properties, (
            f"в опубликованной схеме снова есть load_mode: {sorted(properties)}"
        )

    def test_handler_signature_has_no_load_mode(self, workspace: SessionWorkspace) -> None:
        import inspect

        parameters = inspect.signature(
            ad.create_tool(workspace, execution_timeout_sec=120.0).handler
        ).parameters

        assert "load_mode" not in parameters, sorted(parameters)

    def test_sent_load_mode_is_refused_not_ignored(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        """Принять и проигнорировать — хуже отказа.

        Молча проигнорированный режим означал бы, что модель объявила одно, а
        документ разобран по-другому: ровно то расхождение, ради которого
        параметр и снят.
        """
        handle = workspace.handle("sess-stale", create=True)
        (handle.subdir(ad.FILES_SUBDIR) / DOCUMENT_NAME).write_text(DOCUMENT, encoding="utf-8")
        definition = ad.create_tool(workspace, execution_timeout_sec=120.0)

        with pytest.raises(TypeError) as caught:
            definition.handler(
                _ctx("sess-stale"),
                document=f"session://files/{DOCUMENT_NAME}",
                load_mode="full",
                confirmed=True,
            )

        assert "load_mode" in str(caught.value), caught.value
        assert extraction.modes == [], (
            f"разбор пошёл, хотя режим был не объявлен: {extraction.modes!r}"
        )


class TestEnumCheckSurvivedDerivation:
    """Вывод режима не отменил проверку ``length``."""

    def test_unknown_length_is_refused_before_extraction(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        with pytest.raises(InvalidRequestError) as caught:
            _analyze(workspace, "sess-typo", length="detaield")

        assert caught.value.code == "invalid_params", caught.value.code
        assert extraction.modes == [], (
            f"загрузчик позван при неверном length: {extraction.modes!r}"
        )

    def test_error_names_allowed_lengths(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        with pytest.raises(InvalidRequestError) as caught:
            _analyze(workspace, "sess-typo2", length="detaield")

        message = str(caught.value)
        assert "brief" in message and "detailed" in message, message


class TestOperationStillAnswers:
    """Проба не подменяет разбор целиком: операция обязана отдать ответ."""

    def test_response_is_json_with_operation_id(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        payload = json.loads(_analyze(workspace, "sess-json", length="detailed"))

        assert payload["status"] == "requires_continuation", payload
        assert payload["operation_id"].startswith("op_"), payload
