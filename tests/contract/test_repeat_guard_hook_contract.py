"""Контрактный тест: ``RepeatGuardHook`` против РЕАЛЬНОГО API nanobot 0.3.5.

Зачем отдельный файл, если есть ``tests/test_repeat_guard_hook.py``:
юнит-тесты работают на стабах контекста, а этот — на настоящих типах
nanobot и на настоящем ``_execute_tool_call``. Именно здесь вскрылась
ошибка, которую стабы не видели: upstream вызывает
``_execute_tool_call`` **позиционно**, поэтому извлечение ``hook`` из
``kwargs`` всегда давало ``None`` и ``on_execute_tool_error`` не
вызывался.

Что фиксируется здесь:

  1. Хук — настоящий ``AgentHook`` со всем lifecycle-поверхом.
  2. Полный lifecycle на **пустом** state ничего не ломает, включая
     ``on_execute_tool_error`` — он зовётся и на синтетическом отказе
     в режиме ``block``.
  3. Блокировка НЕ роняет оборот: патч ``repeat_guard_block`` превращает
     ``RepeatGuardBlocked`` в обычный синтетический tool-результат
     ``(payload, event)`` того же вида, что и штатная ошибка инструмента.
  4. Соседние вызовы в том же батче не отменяются.
  5. Чужая ошибка хука НЕ маскируется под отказ защитника.

Запускается через ``pytest -m contract``.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.contract

from nanobot.agent import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.hook import CompositeHook
from nanobot.providers.base import ToolCallRequest

from lib.core.project_settings import GatewayRepeatGuardSettings
from lib.hooks.repeat_guard_hook import RepeatGuardBlocked, RepeatGuardHook


def _run_ctx() -> AgentRunHookContext:
    return AgentRunHookContext(messages=[], final_content="ok", stop_reason="stop")


def _iter_ctx(session_key: str = "contract-1", iteration: int = 0) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[{"role": "user", "content": "go"}],
        session_key=session_key,
    )


def _call(
    name: str = "read_file", arguments: dict | None = None
) -> ToolCallRequest:
    return ToolCallRequest(
        id="tc-1", name=name, arguments=arguments or {"path": "/tmp/x"}
    )


def _hook(**kwargs) -> RepeatGuardHook:
    return RepeatGuardHook(GatewayRepeatGuardSettings(**kwargs))


class _CountingRegistry:
    """Реестр, считающий реальные исполнения инструмента.

    Нужен, чтобы доказать, что отказ защитника происходит ДО обращения к
    инструменту: после срабатывания порога счётчик обязан остаться
    равным единице, а не расти. Любое его дальнейшее увеличение означало бы,
    что блокировка перестала быть точкой решения.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, name, params):
        # Именно async: upstream делает ``await tools.execute(...)``, и
        # синхронный стаб дал бы TypeError вместо проверяемого пути.
        self.calls += 1
        return f"ok:{name}"


@contextmanager
def _guarded_call():
    """Применить патч к РЕАЛЬНОМУ ``_execute_tool_call`` на время теста.

    Откат обязателен: патч глобальный, и оставленный поверх следующего теста
    он замаскировал бы регрессию. ``execute_tool_calls`` обращается к функции
    через глобал модуля, поэтому патч должен быть жив всё время прогона.

    Исходной точкой отсчёта считается функция на входе, а не «настоящая»
    оригинал upstream: другие тесты зовут ``apply_all()``, который патчит
    модуль глобально и не откатывает его, поэтому к моменту запуска этого
    теста атрибут уже может быть обёрнут. Отсюда проверка не на identity, а
    на поведение: обёртка обязана ловить ``RepeatGuardBlocked``.
    """
    from nanobot.agent.tools import execution as exec_mod

    from lib.services.runtime_patcher import RuntimePatcher

    original = exec_mod._execute_tool_call
    try:
        ok, message = RuntimePatcher().patch_repeat_guard_block()
        assert ok, f"патч repeat_guard_block не применился: {message}"
        yield exec_mod._execute_tool_call
    finally:
        exec_mod._execute_tool_call = original


class TestAgentHookSurface:
    def test_is_agent_hook(self) -> None:
        hook = _hook(mode="off")
        assert isinstance(hook, AgentHook)
        # CompositeHook раскручивает lifecycle через getattr по каждому хуку —
        # это ровно тот путь, которым хук попадёт в реальный раннер.
        assert isinstance(CompositeHook([hook]), CompositeHook)

    def test_reraise_is_enabled(self) -> None:
        """``reraise=False`` сделал бы режим ``block`` молчаливым no-op.

        ``HookRegistry._for_each_hook_safe`` глотает исключение каждого хука
        и логирует его: состояние обновилось бы, а вызов всё равно
        выполнился. Защитник, который «вроде блокирует», но не блокирует, —
        хуже, чем отсутствие защитника: он даёт ложную гарантию.
        """
        assert _hook(mode="block")._reraise is True

    def test_full_lifecycle_on_empty_state(self) -> None:
        """Ни один lifecycle-метод не падает на пустом state."""
        hook = _hook(mode="block")
        ctx = _iter_ctx()
        run_ctx = _run_ctx()
        tc = _call()

        async def lifecycle() -> None:
            await hook.before_run(run_ctx)
            await hook.before_iteration(ctx)
            await hook.before_execute_tools(ctx)
            await hook.on_stream(ctx, "chunk")
            await hook.on_stream_end(ctx, resuming=False)
            await hook.after_iteration(ctx)
            await hook.on_execute_tool_error(
                ctx, tc, None, {}, RepeatGuardBlocked("x")
            )
            await hook.after_execute_tool(ctx, tc, None, {}, "ok")
            await hook.on_error(run_ctx)
            await hook.on_finally(run_ctx)
            await hook.after_run(run_ctx)

        asyncio.run(lifecycle())
        assert hook.finalize_content(ctx, "текст") == "текст"
        assert hook.wants_streaming() is False


class TestBlockDoesNotBreakTurn:
    """Патч ``repeat_guard_block`` — то, что делает режим ``block``
    пригодным к применению, а не разрушительным для оборота."""

    def test_blocked_call_returns_synthetic_tool_result(self) -> None:
        with _guarded_call() as guarded:
            hook = _hook(mode="block", window_size=10, max_repeats_in_window=2)
            ctx = _iter_ctx()
            tc = _call()
            registry = _CountingRegistry()

            seen: list[BaseException] = []
            original_error_hook = hook.on_execute_tool_error

            async def spy(context, tool_call, tool, params, error) -> None:
                seen.append(error)
                await original_error_hook(context, tool_call, tool, params, error)

            hook.on_execute_tool_error = spy  # type: ignore[method-assign]

            # Первый вызов проходит, второй упирается в порог.
            asyncio.run(guarded(registry, tc, {}, {}, hook, ctx, dict))
            result, event = asyncio.run(guarded(registry, tc, {}, {}, hook, ctx, dict))

            assert isinstance(result, str)
            assert result.startswith(
                "Error: RepeatGuardBlocked: repeat-guard:"
            ), result
            assert event["status"] == "error"
            assert event["name"] == "read_file"
            assert "repeat-guard" in event["detail"]
            assert registry.calls == 1, (
                "инструмент выполнился после срабатывания защитника — отказ "
                "перестал быть точкой решения"
            )
            assert seen, (
                "on_execute_tool_error обязан быть вызван патчем — upstream "
                "передаёт hook позиционно, и его нельзя брать из kwargs"
            )
            assert isinstance(seen[0], RepeatGuardBlocked)

    def test_sibling_call_is_not_cancelled(self) -> None:
        """В режиме ``concurrent`` отказ одного вызова не роняет соседние.

        ``execute_tool_calls`` собирает батч через ``asyncio.gather`` БЕЗ
        ``return_exceptions=True`` — любое исключение из
        ``_execute_tool_call`` отменяет весь батч. Поэтому блокировка
        обязана превращаться в значение, а не подниматься наружу.
        """
        from nanobot.agent.tools.execution import execute_tool_calls

        with _guarded_call():
            hook = _hook(mode="block", window_size=10, max_repeats_in_window=2)
            ctx = _iter_ctx()
            first = _call("read_file", {"path": "/a"})
            second = _call("read_file", {"path": "/b"})
            registry = _CountingRegistry()

            # Доводим первый вызов до порога: он и дальше будет отклоняться.
            from nanobot.agent.tools import execution as exec_mod

            asyncio.run(exec_mod._execute_tool_call(registry, first, {}, {}, hook, ctx, dict))

            results, events = asyncio.run(
                execute_tool_calls(
                    registry,
                    [first, second],
                    concurrent=True,
                    external_lookup_counts={},
                    workspace_violation_counts={},
                    hook=hook,
                    context=ctx,
                )
            )
            assert len(results) == 2, results
            assert len(events) == 2, events
            # Повтор отклонён, СОСЕДНИЙ вызов с другими аргументами при этом
            # выполнен — ровно то поведение, ради которого отказ должен быть
            # значением, а не исключением: gather без return_exceptions
            # отменил бы весь батч.
            assert results[0].startswith("Error: RepeatGuardBlocked:"), results[0]
            assert results[1] == "ok:read_file", results[1]
            assert events[0]["status"] == "error"
            assert events[1]["status"] == "ok"
            assert events[0]["name"] == "read_file"
            assert events[1]["name"] == "read_file"
            # Один вызов при разогреве + один реально выполненный.
            assert registry.calls == 2, "исполнено не то число вызовов"

    def test_unrelated_hook_error_is_not_masked(self) -> None:
        """Патч ловит ТОЛЬКО ``RepeatGuardBlocked``.

        Чужая ошибка другого хука обязана остаться видимой: замаскировать
        её под «защитник от повторов» — значит спрятать настоящую поломку
        и превратить аварию в тихую блокировку.
        """
        with _guarded_call() as guarded:

            class _Exploding(RepeatGuardHook):
                async def before_execute_tool(self, *args, **kwargs) -> None:
                    raise ValueError("чужой хук сломался")

            boom = _Exploding(GatewayRepeatGuardSettings(mode="off"))
            with pytest.raises(ValueError, match="чужой хук сломался"):
                asyncio.run(
                    guarded(
                        _CountingRegistry(), _call(), {}, {}, boom, _iter_ctx(), dict
                    )
                )


class TestInventoryRegistration:
    def test_hook_in_canonical_inventory(self) -> None:
        from lib.services.runtime_inventory import canonical_framework_hooks

        names = {h.name for h in canonical_framework_hooks()}
        assert "RepeatGuardHook" in names

    def test_patch_registered_in_canonical_inventory(self) -> None:
        from lib.services.runtime_inventory import canonical_runtime_patches

        names = {p.name for p in canonical_runtime_patches()}
        assert "repeat_guard_block" in names
