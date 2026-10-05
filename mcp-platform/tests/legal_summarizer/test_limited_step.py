"""Ограниченный шаг ``batch_limit``: что он обещает и когда не должен.

Шаг режет очередь батчей и возвращает остаток, чтобы разбор не уложившийся в
потолок вызова можно было продолжить. Проверяется ровно то, из-за чего шаг и
заведён: ограничение режет работу, уже оплаченное не переплачивается, и —
главное — **провал внутри шага не выдаётся за «продолжение»**.

Последнее найдено боевой пробой на настоящем документе: упавший батч
возвращался как ``requires_continuation`` с ``continues: true`` и подсказкой
«уже выполненные батчи повторно не оплачиваются», хотя не выполнено ничего и
остаток не уменьшается — упавший батч встаёт в очередь снова. Вызывающий,
который действует по подсказке, крутит вызовы без единого продвижения.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _build_doc(sections: int = 10) -> str:
    """Документ, который даёт несколько контекстных батчей."""
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"Статья {i}. Раздел {i}\n\n"
            + ("Текст положения договора. " * 60) * 60
            + "\n\n"
        )
    return "".join(parts)


def _write_doc(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doc.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _install_llm(monkeypatch, *, fail: bool = False, fail_after: int = 0) -> list[str]:
    """Заглушка батчевого LLM-вызова. Возвращает список выполненных чанков.

    ``fail_after`` — сколько батчей должно пройти успешно, прежде чем
    subsequent начнут падать: так воспроизводится провал при уже оплаченной
    работе, то есть ветка ``partial``, а не ``failed``.
    """
    import libs.legal_summarizer.llm.calls as llm_calls

    done: list[str] = []

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        if fail and len(done) >= fail_after:
            raise llm_calls.ChunkResultParseError("подсаженный провал батча")
        for c in chunks:
            done.append(c.chunk_id)
        return {c.chunk_id: f"сводка чанка {c.chunk_id}" for c in chunks}

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(
        llm_calls, "llm_section_reduce", lambda *a, **k: "сводка раздела"
    )
    monkeypatch.setattr(
        llm_calls,
        "llm_document_reduce",
        lambda *a, **k: "сводка документа",
    )
    return done


def _run(text: str, doc: Path, tmp_path: Path, **kwargs):
    import libs.legal_summarizer.application.service as summarizer

    return summarizer.run(
        text,
        length="detailed",
        document_path=str(doc),
        workspace_root=tmp_path,
        confirmed=True,
        **kwargs,
    )


class TestLimitedStepBoundsWork:
    def test_limit_bounds_the_step_and_leaves_a_remainder(self, tmp_path, monkeypatch):
        done = _install_llm(monkeypatch)
        text = _build_doc()
        doc = _write_doc(tmp_path, text)

        outcome = _run(text, doc, tmp_path, batch_limit=1)

        assert outcome["status"] == "requires_continuation", outcome
        stats = outcome["stats"]
        # Ветка ограниченного шага обязана быть задета, иначе проверки ниже
        # проходят на любом ответе и ничего не доказывают.
        assert stats["deferred_batches"] >= 1, stats
        report = outcome["progress_report"]
        assert report["continues"] is True
        assert report["done"] >= 1, report
        assert report["remaining"] >= 1, report
        assert stats["total_llm_calls"] >= 1

    def test_limited_step_writes_no_result_and_leaves_state_resumable(
        self, tmp_path, monkeypatch
    ):
        _install_llm(monkeypatch)
        text = _build_doc()
        doc = _write_doc(tmp_path, text)

        first = _run(text, doc, tmp_path, batch_limit=1)
        assert first["stats"]["deferred_batches"] >= 1
        operation_id = first["operation_id"]

        from libs.legal_summarizer.cache.manifest import manifest_path

        manifest = manifest_path(operation_id, tmp_path)
        assert manifest.is_file(), "шаг обязан оставить состояние, а не только ответ"
        # result.json не появился: иначе короткое замыкание на `completed`
        # заморозило бы усечённый результат навсегда.
        assert not (manifest.parent / "result.json").is_file()

        # Продолжение платит только за остаток: уже выполненные чанки в
        # очередь не попадают.
        done = _install_llm(monkeypatch)
        again = _run(text, doc, tmp_path, batch_limit=1)
        already = set(done)
        assert already, "продолжение обязано что-то выполнить"
        assert again["operation_id"] == operation_id
        assert set(done).issuperset(already)

    def test_unset_limit_keeps_full_parse(self, tmp_path, monkeypatch):
        _install_llm(monkeypatch)
        text = _build_doc()
        doc = _write_doc(tmp_path, text)

        outcome = _run(text, doc, tmp_path, batch_limit=None)

        assert outcome["status"] == "completed", outcome
        stats = outcome["stats"]
        # Полный разбор шёл не ограниченным шагом: маркер остатка есть только
        # у него, а здесь его быть не должно.
        assert "deferred_batches" not in stats, stats
        # Ограничение не заданное — прежнее поведение: все батчи выполнены,
        # плюс reduce-фаза.
        assert stats["map_calls"] == stats["context_batches_total"] >= 10, stats
        assert stats["reduce_calls"] >= 1, stats


class TestFailedBatchIsNotReportedAsContinuation:
    """Провал внутри ограниченного шага — это отказ, а не «продолжи потом»."""

    def test_failed_batch_without_any_progress_returns_failed(self, tmp_path, monkeypatch):
        _install_llm(monkeypatch, fail=True, fail_after=0)
        text = _build_doc()
        doc = _write_doc(tmp_path, text)

        outcome = _run(text, doc, tmp_path, batch_limit=1)

        # Ветка ограниченного шага обязана быть задета — иначе проверка ниже
        # прошла бы на обычном отказе по NO_PARTIALS и доказывала бы не то.
        assert outcome["stats"]["deferred_batches"] >= 1, outcome["stats"]
        assert outcome["status"] == "failed", (
            "упавший батч не должен выдаваться за requires_continuation: остаток "
            f"не уменьшается, вызов за вызовом получит то же состояние. Ответ: {outcome}"
        )
        # Ничего не выполнено — обещать остаток нельзя, поэтому и отчёт о
        # прогрессе не отдаётся.
        assert "progress_report" not in outcome, outcome
        # Причина обязана дойти до вызывающего, иначе он не знает, что чинить.
        assert outcome.get("error", {}).get("code"), outcome
        assert outcome.get("hint") is None, (
            "подсказка про «уже выполненные батчи повторно не оплачиваются» "
            "при полном провале уводит в петлю"
        )

    def test_failed_batch_after_paid_work_returns_partial(self, tmp_path, monkeypatch):
        text = _build_doc()
        doc = _write_doc(tmp_path, text)

        # Первый шаг платит и оставляет состояние, второй встречается с провалом.
        _install_llm(monkeypatch)
        first = _run(text, doc, tmp_path, batch_limit=1)
        assert first["status"] == "requires_continuation", first
        done_before = first["progress_report"]["done"]
        assert done_before >= 1

        _install_llm(monkeypatch, fail=True, fail_after=0)
        second = _run(text, doc, tmp_path, batch_limit=1)

        assert second["operation_id"] == first["operation_id"]
        assert second["stats"]["deferred_batches"] >= 1, second["stats"]
        # `partial` занят смыслом частичного провала — ровно этот случай.
        assert second["status"] == "partial", second
        report = second["progress_report"]
        assert report["continues"] is False, (
            "продолжение после провала невозможно до устранения причины: тот же "
            "батч снова упадёт"
        )
        assert report["done"] >= done_before, report