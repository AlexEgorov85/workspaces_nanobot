"""Ответ модели без текста — отказ вызова, а не «свод действительно пуст».

Проверяется:

1. ``chat`` поднимает отказ, когда провайдер вернул только рассуждение
   (``<think>…</think>`` без ответа) или не вернул ничего. Это тот самый
   боевой отказ 2026-10-06: модель ``MiniMax-M3`` на своде Конституции.
2. Код этого отказа **повторяемый платформой**, иначе модель не повторит
   вызов, а документ останется неразобранным навсегда.
3. Инвариант, который до правки не был обеспечен: отказ вызова не доходит до
   ``REDUCE_INPUT_EMPTY`` ни на одной ветке сведения свода.
4. Потолок ответа reduce-вызовов учитывает расход модели на рассуждение.

Невакуумность: снять проверку в ``chat`` — красные пробы 1-3; вернуть
голый номинал ``max_tokens`` без резерва — красная проба 4; вернуть
неповторяемый код — красные пробы 2-3.
"""

from __future__ import annotations

import pytest

from libs.enterprise_common.execution.errors import FAILURE_CODES, RETRYABLE_CODES


def _declare(config_mod, llm: dict) -> None:
    """Объявить доменные настройки LLM объявленным способом.

    Через конфигурацию домена, а не подменой функции: потолок обязан читаться
    из настроек оборота, и подмена ``get_llm_config`` проверила бы только то,
    что подмена работает.
    """
    from dataclasses import replace

    config_mod.configure(replace(config_mod.current(), llm=llm))


def _chat_with(monkeypatch, response: str, **kwargs) -> str:
    """Позвать ``client.chat`` с подменённым провайдером."""
    from libs.legal_summarizer.llm import client as client_mod

    calls: list[dict] = []

    def _fake_complete(messages, context=None, **kw):
        calls.append(kw)
        return response

    monkeypatch.setattr(client_mod, "complete", _fake_complete)
    return client_mod.chat(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        **kwargs,
    )


class TestAnswerWithoutTextIsCallFailure:
    """Пустой ответ на проводе — это отказ, а не результат."""

    def test_reasoning_only_answer_is_refused(self, monkeypatch) -> None:
        """Ровно боевой отказ: рассуждение есть, ответа нет."""
        from libs.legal_summarizer.llm.client import LlmEmptyResponse

        with pytest.raises(LlmEmptyResponse) as excinfo:
            _chat_with(
                monkeypatch,
                "<think>Пользователь просит краткий пересказ. Подумаем.\n"
                "Что тут вообще нужно сделать?</think>",
                max_tokens=2000,
            )

        assert "2000" in str(excinfo.value), (
            "сообщение должно называть потолок: именно он обычно и съеден "
            "рассуждением"
        )

    def test_unfinished_reasoning_is_refused(self, monkeypatch) -> None:
        """``</think>`` не закрыт — ответа всё равно нет."""
        from libs.legal_summarizer.llm.client import LlmEmptyResponse

        with pytest.raises(LlmEmptyResponse):
            _chat_with(monkeypatch, "<think>рассуждение без разрыва и без ответа")

    def test_empty_answer_is_refused(self, monkeypatch) -> None:
        from libs.legal_summarizer.llm.client import LlmEmptyResponse

        with pytest.raises(LlmEmptyResponse):
            _chat_with(monkeypatch, "")

    def test_whitespace_answer_is_refused(self, monkeypatch) -> None:
        from libs.legal_summarizer.llm.client import LlmEmptyResponse

        with pytest.raises(LlmEmptyResponse):
            _chat_with(monkeypatch, "   \n  ")

    def test_answer_is_returned_unchanged(self, monkeypatch) -> None:
        """Ответ уходит в том виде, в каком пришёл.

        Проверка обязана быть на форме: «починить» пустой ответ можно,
        сняв кусок разбора у вызывающих, и это не будет дефектом с точки зрения
        этой строки.
        """
        raw = "<think>размышление</think>Итог: документ изложен."

        assert _chat_with(monkeypatch, raw) == raw


class TestEmptyAnswerIsRetryableForThePlatform:
    """Повторяемость берётся у платформы, а не назначается доменом."""

    def test_code_is_known_to_the_platform(self) -> None:
        from libs.legal_summarizer.llm.client import LlmEmptyResponse, classify_llm_failure

        code = classify_llm_failure(LlmEmptyResponse("пусто"))

        assert code in FAILURE_CODES, f"{code} платформа не знает — она перепишет его в internal"
        assert code in RETRYABLE_CODES, (
            f"{code} неповторяемый: тот же документ через минуту разбирается, "
            "и модель обязана получить право повторить"
        )

    def test_direct_branch_reports_call_failure_not_empty_summary(
        self, monkeypatch
    ) -> None:
        """Инвариант на ветке ``direct``: одна ошибка, а не «свод пуст»."""
        from libs.legal_summarizer.application import execution_orchestration as orch
        from libs.legal_summarizer.llm.client import LlmEmptyResponse

        def _refuse(*args, **kwargs):
            raise LlmEmptyResponse("модель не вернула текста ответа")

        monkeypatch.setattr(orch, "_llm_calls_mod", type("M", (), {
            "llm_document_reduce": staticmethod(_refuse),
        }))

        class _Chunk:
            chunk_id = "c0"
            text = "текст чанка"

        outcome = orch.run_direct(
            [_Chunk()],
            length="brief",
            focus=None,
            question=None,
            structure=None,
            analysis=None,
            operation_id="op-empty-answer",
            document_path=None,
            workspace_root=None,
            chars_in=100,
            estimated_llm_calls=1,
            article_count=0,
            existing_manifest=None,
        )

        code = (outcome.get("error") or {}).get("code")
        assert code != "REDUCE_INPUT_EMPTY", (
            "отказ вызова выдан как «свод действительно пуст»: это решение "
            "было принято, но код его не обеспечивал"
        )
        assert code in RETRYABLE_CODES
        assert outcome["error"]["stage"] == "document_reduce"


class TestCeilingLeavesRoomForReasoning:
    """Потолок ответа считается на содержимое **и** на рассуждение."""

    def test_reserve_is_added_to_nominal(self) -> None:
        from libs.legal_summarizer.llm import calls as calls_mod

        assert calls_mod._reduce_max_tokens(2000) == 2000 + 4096

    def test_ceiling_of_platform_is_respected(self) -> None:
        """Больше, чем провайдер готов отдать, просить бессмысленно."""
        from libs.legal_summarizer.llm import calls as calls_mod
        from libs.legal_summarizer.llm import config as config_mod

        _declare(config_mod, {"max_tokens": 2500, "reasoning_reserve_tokens": 4096})
        try:
            assert calls_mod._reduce_max_tokens(2000) == 2500
        finally:
            config_mod.reset()

    def test_reserve_is_read_from_settings(self) -> None:
        """Резерв приходит настройкой, а не зашит в код."""
        from libs.legal_summarizer.llm import calls as calls_mod
        from libs.legal_summarizer.llm import config as config_mod

        _declare(config_mod, {"max_tokens": 8192, "reasoning_reserve_tokens": 0})
        try:
            assert calls_mod._reduce_max_tokens(2000) == 2000
        finally:
            config_mod.reset()

    def test_reduce_calls_pass_the_computed_ceiling(self, monkeypatch) -> None:
        """Проверяется на вызове, а не в формуле: потолок легко забыть."""
        from libs.legal_summarizer.llm import calls as calls_mod

        seen: list[int | None] = []

        def _fake_chat(messages, context=None, **kw):
            seen.append(kw.get("max_tokens"))
            return "свод"

        monkeypatch.setattr(calls_mod.llm, "chat", _fake_chat)

        calls_mod.llm_section_reduce("Глава 1", "Глава 1", "часть свода", length="brief")
        calls_mod.llm_document_reduce("своды разделов", length="brief", focus=None, structure=None)

        assert seen == [2000 + 4096, 3000 + 4096]