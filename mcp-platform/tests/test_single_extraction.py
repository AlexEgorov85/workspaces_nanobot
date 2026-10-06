"""Документ читается целиком и одинаково: режим извлечения не существует.

Правило, которое держит страж. Структура документа строится **без LLM**, а
значит строить её по-разному в зависимости от режима свода незачем и вредно:
получается две разные структуры одного файла. Раньше так и было — у загрузчика
стоял режим ``brief``, который для PDF резал документ до 100 страниц и
300 000 символов через pypdf, и операция выводила его из ``length``. На
документе длиннее потолка структура строилась по усечённому тексту, то есть
outline описывал только начало, и модель отвечала уверенно про документ,
который видела наполовину. Молчаливая ложь вместо отказа.

Различаться должен свод, а не объём прочитанного:

* ``brief`` — один структурный chunk: outline плюс выжимки верхнего уровня,
  один проход по LLM, ограничение задаёт сборка chunk'а (окно модели и
  ``structure_max_chars`` на outline);
* ``detailed`` — весь документ, нарезанный по структуре на чанки, map-reduce.

Проверки невакуумны по построению:

* ``chars_in`` сверяется с длиной файла на тексте длиннее прежнего потолка,
  поэтому любая возвращённая обрезка даёт красное, а не «просто работает»;
* проба загрузчика требует, чтобы режим **не передавался** (пустой набор
  аргументов), а не «передался любой»;
* большой текст строится настоящей строкой, и её длина проверяется до вызова
  операции, чтобы проба не позеленела на пустом документе.
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
from libs.legal_summarizer.application import document_io  # noqa: E402
from servers.enterprise.tools import analyze_document as ad  # noqa: E402

#: Больше прежнего потолка «100 страниц / 300 000 символов», чтобы обрезка
#: на уровне извлечения была видна числом, а не догадкой.
BIG_CHARS = 300_137

_BIG_TEXT = "Статья 1. " + ("тело статьи. " * 24_000)
assert len(_BIG_TEXT) >= BIG_CHARS, len(_BIG_TEXT)


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


class _ExtractionProbe:
    """Загрузчик домена, записывающий, что именно ему передали.

    Подпись объявлена явно, а не ``**kwargs``: подмена с ``**kwargs``
    приняла бы вызов с любым набором аргументов, включая ``mode``, и проба
    позеленела бы вхолостую.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.text: str = ""

    def load_text(self, path: Path) -> str:
        # Режима быть не должно: принимать ``**kwargs`` здесь запрещено, иначе
        # его повторное появление не было бы замечено.
        self.calls.append((Path(path).name, {}))
        return self.text


class _DomainProbe:
    """Домен, который отвечает без платной работы и записывает вызовы."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

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
        self.calls.append((length, len(text)))
        return {
            "status": "requires_continuation",
            "summary": {
                "chars_in": len(text),
                "chunks": 1,
                "context_batches_total": 1,
            },
        }


@pytest.fixture()
def domain(monkeypatch: pytest.MonkeyPatch) -> _DomainProbe:
    probe = _DomainProbe()
    monkeypatch.setattr(
        "libs.legal_summarizer.application.service.run",
        probe.run,
        raising=True,
    )
    return probe


@pytest.fixture()
def extraction(monkeypatch: pytest.MonkeyPatch, domain: _DomainProbe) -> _ExtractionProbe:
    probe = _ExtractionProbe()
    probe.text = _BIG_TEXT
    monkeypatch.setattr(
        "libs.legal_summarizer.application.document_io.load_text",
        probe.load_text,
        raising=True,
    )
    return probe


def _ctx(session_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id="req-single", session_id=session_id, user_id="u-single"
        ),
        tool_name="platform.analyze_document",
        capability="platform",
        started_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
    )


def _analyze(workspace: SessionWorkspace, session_id: str, name: str, **kwargs: Any) -> dict:
    handle = workspace.handle(session_id, create=True)
    (handle.subdir(ad.FILES_SUBDIR) / name).write_text("файл", encoding="utf-8")
    definition = ad.create_tool(workspace, execution_timeout_sec=120.0)
    return json.loads(
        definition.handler(
            _ctx(session_id),
            document=f"session://files/{name}",
            confirmed=True,
            **kwargs,
        )
    )


class TestExtractionHasNoMode:
    """Режима извлечения нет ни в загрузчике, ни в модуле."""

    def test_loader_signature_has_no_mode(self) -> None:
        import inspect

        parameters = inspect.signature(document_io.load_text).parameters

        assert "mode" not in parameters, sorted(parameters)

    def test_page_head_extractor_is_gone(self) -> None:
        """Обрезка первых страниц удалена вместе с режимом.

        Не «спрятана в другом месте»: если потолок вернётся другим именем,
        эта проверка молча перестанет что-то значить, поэтому она же
        требует, чтобы модуль вообще не держал приватных обрезателей.
        """
        assert not hasattr(document_io, "_extract_pdf_head"), (
            "вернулась обрезка первых страниц — структура снова будет строиться "
            "по усечённому тексту"
        )

    def test_mode_is_refused(self, tmp_path: Path) -> None:
        p = tmp_path / "doc.txt"
        p.write_text("Статья 1.", encoding="utf-8")

        with pytest.raises(TypeError) as caught:
            document_io.load_text(p, mode="brief")

        assert "mode" in str(caught.value), caught.value


class TestOperationReadsWholeDocument:
    """Никакой обрезки на уровне операции."""

    def test_chars_in_is_the_whole_file(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe, domain: _DomainProbe
    ) -> None:
        payload = _analyze(workspace, "sess-big", "big.txt", length="brief")

        assert payload["status"] == "requires_continuation", payload
        assert domain.calls, "домен не вызван — проба ничего не проверяет"
        seen = domain.calls[-1][1]
        assert seen == len(_BIG_TEXT), (
            f"в домен ушло {seen} симв. из {len(_BIG_TEXT)}: текст обрезан по дороге"
        )
        assert payload["summary"]["chars_in"] == len(_BIG_TEXT), payload["summary"]

    def test_loader_is_called_without_a_mode(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        """Проба фиксирует отсутствие режима, а не его значение."""
        _analyze(workspace, "sess-nomode", "big.txt", length="detailed")

        assert extraction.calls, "загрузчик не вызван — проба ничего не проверяет"
        for name, kwargs in extraction.calls:
            assert kwargs == {}, f"загрузчику передан лишний набор: {name} {kwargs}"


class TestBothLengthsReadTheSameThing:
    """Правило владельца: структура одна, различается свод."""

    def test_both_lengths_get_the_same_text(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe, domain: _DomainProbe
    ) -> None:
        _analyze(workspace, "sess-brief", "big.txt", length="brief")
        _analyze(workspace, "sess-detailed", "big.txt", length="detailed")

        assert len(domain.calls) == 2, domain.calls
        brief_len, detailed_len = domain.calls[0][1], domain.calls[1][1]
        assert brief_len == detailed_len, (
            f"краткий прочитал {brief_len}, подробный {detailed_len}: "
            f"извлечение зависит от режима"
        )
        assert brief_len == len(_BIG_TEXT)

    def test_lengths_still_reach_the_domain(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe, domain: _DomainProbe
    ) -> None:
        """Одно извлечение не должно сливать режимы свода.

        ``length`` обязан дойти до домена: если бы он тоже стал «одним», то
        краткий и подробный свод стали бы одним и тем же разбором.
        """
        for length in ("brief", "detailed"):
            _analyze(workspace, f"sess-len-{length}", "big.txt", length=length)

        assert [c[0] for c in domain.calls] == ["brief", "detailed"], domain.calls


class TestModelSurfaceIsOneKnob:
    """На поверхности модели — ``length`` и ничего больше."""

    def test_published_schema_has_no_load_mode(self, workspace: SessionWorkspace) -> None:
        properties = ad.create_tool(workspace, execution_timeout_sec=120.0).input_schema[
            "properties"
        ]

        # Невакуумность: пустая схема прошла бы «параметра нет».
        assert properties, "схема операции пуста"
        assert "length" in properties, sorted(properties)
        assert "load_mode" not in properties, sorted(properties)

    def test_sent_load_mode_is_refused(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        handle = workspace.handle("sess-stale", create=True)
        (handle.subdir(ad.FILES_SUBDIR) / "big.txt").write_text("файл", encoding="utf-8")
        definition = ad.create_tool(workspace, execution_timeout_sec=120.0)

        with pytest.raises(TypeError) as caught:
            definition.handler(
                _ctx("sess-stale"),
                document="session://files/big.txt",
                load_mode="full",
                confirmed=True,
            )

        assert "load_mode" in str(caught.value), caught.value
        assert extraction.calls == [], "разбор пошёл, хотя режим не объявлен"

    def test_unknown_length_is_still_refused(
        self, workspace: SessionWorkspace, extraction: _ExtractionProbe
    ) -> None:
        with pytest.raises(InvalidRequestError) as caught:
            _analyze(workspace, "sess-typo", "big.txt", length="detaield")

        assert caught.value.code == "invalid_params", caught.value.code
        assert extraction.calls == [], "загрузчик позван при неверном length"