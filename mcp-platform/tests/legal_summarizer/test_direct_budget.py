"""Потолок вызова на ветке ``direct``: неделимый шаг объявляет отказ.

Ветка ``direct`` — единственная, где потолок вызова нечем соблюдать резанием:
батчей нет, резать нечего, а ``ExecutionTimeout`` обрывает уже начатый и
оплаченный вызов. Поэтому невписавшийся в потолок шаг домен обязан объявить
отказом ``legal_budget_unreachable`` сам — до единого LLM-вызова и до записи
состояния.

Проверяется ровно это, а не «параметр существует»:

* отказ приходит при **нуле** LLM-вызовов (считает заглушка, а не код);
* отказ не оставляет состояния: ни манифеста, ни ``result.json``;
* при потолке, в который шаг помещается, ветка ведёт себя как раньше;
* ``None`` — прежнее поведение;
* граница строгая: потолок, **равный** верхней оценке, отказа не даёт;
* на ветке с батчами параметр игнорируется: там ту же работу режет
  ``batch_limit``, и решение о пределении шага принадлежит ему одному;
* гейт подтверждения остаётся первым: потолок не узурбляет решение
  «начинать ли вовсе».

Каждая проба сначала убеждается, что попала на заявленную ветку
(``ctx.strategy`` / ``ctx.plan`` строятся доменом и сверяются в тесте), иначе
проверка отказа прошла бы на ветке с батчами и ничего не доказывала.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_doc(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doc.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _tiny_doc() -> str:
    """Один чанк без членения — прямой неделимый шаг."""
    return "Только один абзац текста, без секций."


def _batched_doc(sections: int = 10) -> str:
    """Документ с несколькими чанками и членением — ветка с батчами."""
    parts = []
    for index in range(1, sections + 1):
        parts.append(
            f"Статья {index}. Раздел {index}\n\n"
            + ("Текст положения договора. " * 60) * 60
            + "\n\n"
        )
    return "".join(parts)


def _install_llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Заглушки LLM со счётчиками по каждому виду вызова.

    Счётчики — прибор пробы: «ноль платной работы» утверждается ими, а не
    отсутствием исключения. ``sum(calls.values()) == 0`` означает, что не
    вызвано ничего: ни батча, ни reduce'а раздела, ни reduce'а документа.
    """
    import libs.legal_summarizer.llm.calls as llm_calls

    calls = {"batch": 0, "section_reduce": 0, "document_reduce": 0}

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        calls["batch"] += 1
        return {chunk.chunk_id: f"сводка {chunk.chunk_id}" for chunk in chunks}

    def _fake_section(*args, **kwargs):
        calls["section_reduce"] += 1
        return "сводка раздела"

    def _fake_document(*args, **kwargs):
        calls["document_reduce"] += 1
        return "сводка документа"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_document)
    return calls


def _assert_direct_estimate_max(text: str, doc: Path) -> float:
    """Подтвердить ветку ``direct`` и вернуть верхнюю границу её оценки.

    Оценка берётся из оценки самого домена: проба интересует отказом, а не
    оценкой (оценка закрыта своими прогонами), и значение нужно, чтобы задать
    потолок заведомо ниже и заведомо выше этой границы.

    Проверка ветки обязана быть в пробе: иначе отказ
    ``legal_budget_unreachable`` прошёл бы на документе с батчами, а такой
    проба ничего не доказывала бы.
    """
    import libs.legal_summarizer.application.service as summarizer

    insp = summarizer.inspect(text, document_path=str(doc))
    ctx = summarizer.build_execution_context(insp, length="detailed", question=None)
    assert ctx.strategy == "direct", f"проба ушла не на ту ветку: {ctx.strategy}"
    assert ctx.plan is None, f"у direct неделимости нет: {ctx.plan}"

    estimate_max = summarizer.estimate_for_run(insp, ctx).estimated_duration_max_sec
    assert estimate_max > 0, (
        "нулевая оценка сделала бы потолок бессмысленным: любой положительный "
        "проходил бы, и проба отказа ничего не проверяла бы"
    )
    return estimate_max


def _run(text: str, doc: Path, root: Path, **kwargs):
    import libs.legal_summarizer.application.service as summarizer

    return summarizer.run(
        text,
        length="detailed",
        document_path=str(doc),
        workspace_root=root,
        confirmed=True,
        **kwargs,
    )


def _low_threshold(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Опустить порог подтверждения, не трогая остальную политику.

    Нужно, чтобы гейт подтверждения действительно срабатывал: при умолчании он
    на ветке ``direct`` молчит (оценка ниже порога), и проба «гейт остаётся
    первым» прошла бы, ни разу не встретив гейт.
    """
    import libs.legal_summarizer.llm.config as llm_config

    real_config = llm_config.get_execution_config

    def _patched():
        return {**real_config(), "confirmation_threshold_sec": seconds}

    monkeypatch.setattr(llm_config, "get_execution_config", _patched)


class TestDirectRefusesUnreachableStep:
    """Потолок ниже оценки неделимого шага — отказ, а не обрыв по таймауту."""

    def test_refusal_comes_before_any_llm_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        estimate_max = _assert_direct_estimate_max(text, doc)

        outcome = _run(text, doc, tmp_path, call_budget_sec=estimate_max / 2)

        assert outcome["status"] == "failed", outcome
        assert outcome["error"]["code"] == "legal_budget_unreachable", outcome
        # Прибор пробы: платной работы не было. Без этого утверждения отказ
        # «после единого вызова» прошёл бы, потому что код верен.
        assert sum(calls.values()) == 0, calls

    def test_refusal_names_the_code_and_promises_no_continuation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        estimate_max = _assert_direct_estimate_max(text, doc)

        outcome = _run(text, doc, tmp_path, call_budget_sec=estimate_max / 2)

        assert outcome["status"] == "failed", outcome
        assert outcome["error"]["code"] == "legal_budget_unreachable"
        assert outcome["error"]["message"], "отказ без объяснения не чинится"
        # Остатка нет, а «продолжить потом» здесь нечем: отчёт о прогрессе
        # на отказе — это обещание, которое выполнить нельзя.
        assert "progress_report" not in outcome, outcome
        assert "result" not in outcome, outcome

    def test_refusal_leaves_no_state_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        estimate_max = _assert_direct_estimate_max(text, doc)

        outcome = _run(text, doc, tmp_path, call_budget_sec=estimate_max / 2)
        operation_id = str(outcome["operation_id"])

        from libs.legal_summarizer.cache.manifest import (
            load_manifest,
            manifest_path,
        )

        # Состояния нет вообще, а не «начатое»: манифест в статусе running
        # остался бы висеть и уводил бы читателя обещанием работы.
        assert load_manifest(operation_id, tmp_path) is None, operation_id
        assert not manifest_path(operation_id, tmp_path).exists()
        assert not (
            manifest_path(operation_id, tmp_path).parent / "result.json"
        ).exists()


class TestDirectKeepsWorkingWithinTheBudget:
    """Потолок, в который шаг помещается, — прежнее поведение ветки."""

    def test_generous_budget_completes_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        estimate_max = _assert_direct_estimate_max(text, doc)

        outcome = _run(text, doc, tmp_path, call_budget_sec=estimate_max * 10)

        assert outcome["status"] == "completed", outcome
        assert "error" not in outcome, outcome
        assert outcome["stats"]["strategy"] == "direct", outcome["stats"]
        # Шаг действительно отработал: без этой проверки проба прошла бы и на
        # тихом отказе с другим кодом.
        assert calls["document_reduce"] == 1, calls
        report = outcome["progress_report"]
        assert (report["done"], report["remaining"], report["continues"]) == (
            1,
            0,
            False,
        ), report

    def test_budget_equal_to_estimate_is_not_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        estimate_max = _assert_direct_estimate_max(text, doc)

        outcome = _run(text, doc, tmp_path, call_budget_sec=estimate_max)

        # Граница строгая: «помещается» значит «помещается», а не «влезло с
        # запасом». Отказ на равенстве отнимал бы у вызывающего ровно тот
        # потолок, который он и объявил.
        assert outcome["status"] == "completed", outcome
        assert calls["document_reduce"] == 1, calls

    def test_absent_budget_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        _assert_direct_estimate_max(text, doc)

        outcome = _run(text, doc, tmp_path, call_budget_sec=None)

        assert outcome["status"] == "completed", outcome
        assert calls["document_reduce"] == 1, calls


class TestBudgetDoesNotTouchOtherBranches:
    """Параметр принадлежит ветке ``direct`` и более ни одной."""

    def test_batched_branch_ignores_call_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import libs.legal_summarizer.application.service as summarizer

        calls = _install_llm(monkeypatch)
        text = _batched_doc()
        doc = _write_doc(tmp_path, text)
        # Ветка обязана быть настоящей: срез батчей делает ``batch_limit``, и
        # потолок вызова там — второй владелец того же решения.
        insp = summarizer.inspect(text, document_path=str(doc))
        ctx = summarizer.build_execution_context(insp, length="detailed")
        assert ctx.plan is not None, "проба ушла на ветку без батчей"
        assert ctx.strategy != "direct", ctx.strategy

        outcome = _run(text, doc, tmp_path, call_budget_sec=1.0)

        assert outcome["status"] == "completed", outcome
        assert "error" not in outcome, outcome
        assert outcome["stats"]["map_calls"] >= 1, outcome["stats"]
        assert calls["batch"] >= 1, calls

    def test_confirmation_gate_still_comes_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Потолок не узурбляет решение «начинать ли вовсе».

        Порог опущен так, что гейт срабатывает на неделимом шаге. Отказ по
        потолку не должен подменять собой подтверждение: подтверждение —
        решение о платной работе вообще, а потолок — ограничение уже
        начатой работы.
        """
        calls = _install_llm(monkeypatch)
        text = _tiny_doc()
        doc = _write_doc(tmp_path, text)
        estimate_max = _assert_direct_estimate_max(text, doc)
        _low_threshold(monkeypatch, estimate_max / 2)

        import libs.legal_summarizer.application.service as summarizer

        outcome = summarizer.run(
            text,
            length="detailed",
            document_path=str(doc),
            workspace_root=tmp_path,
            confirmed=False,
            call_budget_sec=1.0,
        )

        assert outcome["status"] == "confirmation_required", outcome
        assert "error" not in outcome, outcome
        assert outcome["progress_report"]["done"] == 0, outcome
        assert sum(calls.values()) == 0, calls