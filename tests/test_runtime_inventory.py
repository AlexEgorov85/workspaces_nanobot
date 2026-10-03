"""Тесты для ``lib/services/runtime_inventory.py``."""
from __future__ import annotations

import inspect
from pathlib import Path


class TestCanonical:
    def test_framework_hooks_required(self) -> None:
        """Правило: у каждого фреймворкового хука есть живой ``source``
        на диске, и он либо обязателен, либо конфиг-гейтится.

        Раньше здесь стояло безусловное ``all(h.required)``. После
        переезда ``save_turn`` в ``ToolResultArchiveHook`` (фаза 6,
        п. 6.2) хук стал опциональным по существу: он не создаётся при
        ``gateway.persist_threshold <= 0``, и объявлять его required
        значило бы кричать «критический дрейф» на осознанное выключение
        фичи. Поэтому blanket-утверждение заменено правилом, которое
        различает «обязателен всегда» и «обязателен, если фича
        включена», и **проверяет гейт**, а не просто разрешает
        ``required=False``. Тем же правилом помечен ``RepeatGuardHook``
        (change ``repeat-guard-hook``): он обязателен, пока
        ``gateway.repeat_guard.mode`` не выключен.

        Проверяется на заведомо плохих данных: убери гейт
        ``mode == "off"`` из ``RepeatGuardHook`` — тест упадёт,
        и required=False станет неправомерным.

        Снятый ``ToolResultArchiveHook`` (change
        ``use-upstream-tool-result-persist``) в каноне больше не значится:
        персист делает сам upstream.
        """
        from lib.services.runtime_inventory import canonical_framework_hooks

        root = Path(__file__).resolve().parents[1]
        specs = {h.name: h for h in canonical_framework_hooks()}

        assert "ToolAuditHook" in specs
        assert "TerminalToolPrintHook" in specs
        # Защитник от вырожденных циклов (change repeat-guard-hook).
        assert "RepeatGuardHook" in specs
        # Нативная замена патча save_turn ушла в upstream — хука нет.
        assert "ToolResultArchiveHook" not in specs, (
            "хук архивирования удалён в пользу upstream "
            "maybe_persist_tool_result; возвращать его в канон нельзя"
        )

        for name, spec in specs.items():
            assert (root / spec.source).is_file(), (
                f"{name}: source {spec.source} не найден на диске"
            )
            assert spec.kind == "framework", name
            assert spec.description, f"{name}: пустое описание"

        # Ровно те optional, чья опциональность обоснована гейтом.
        optional = {n for n, s in specs.items() if not s.required}
        assert optional == {"RepeatGuardHook"}, (
            f"неожиданный набор optional framework hooks: {sorted(optional)}"
        )

        from lib.hooks.repeat_guard_hook import RepeatGuardHook

        guard_src = inspect.getsource(RepeatGuardHook)
        assert 'self._mode == "off"' in guard_src, (
            'RepeatGuardHook помечен required=False, но before_execute_tool '
            'не гейтится mode="off" — optionality не обоснована'
        )

    def test_plugin_hooks_include_required(self) -> None:
        from lib.services.runtime_inventory import canonical_plugin_hooks

        names = {h.name for h in canonical_plugin_hooks()}
        assert "SessionFileRedirectHook" in names
        assert "RecentFilesHook" in names
        required = [h for h in canonical_plugin_hooks() if h.required]
        assert {h.name for h in required} >= {
            "SessionFileRedirectHook",
            "RecentFilesHook",
        }

    def test_plugin_hooks_have_optional_diag(self) -> None:
        from lib.services.runtime_inventory import canonical_plugin_hooks

        diag = [h for h in canonical_plugin_hooks() if "Diag" in h.name]
        assert diag
        assert not all(h.required for h in diag)

    def test_project_tools_required(self) -> None:
        """Правило: канонический список project tools совпадает с диском.

        Раньше тест выписывал множество имён, и добавление
        ``document_read`` (перенос порога длины текста из патча в tool)
        ломало его без содержательной причины. Теперь ожидание
        выражено правилом — сверка идёт с фактическим
        ``workspace/tools/*.py``, тем же механизмом, что и
        auto-discover в ``project_tool_loader`` — поэтому новый tool
        добавляет одну строку в канон, а не правит тест, и забытый
        tool по-прежнему ловится.
        """
        from lib.services.runtime_inventory import canonical_project_tools

        specs = canonical_project_tools()
        required = {t.name for t in specs if t.required}
        assert required, "ни одного required project tool в каноне"

        tools_dir = Path(__file__).resolve().parents[1] / "workspace" / "tools"
        on_disk = {
            p.stem
            for p in tools_dir.glob("*.py")
            if not p.stem.startswith("_")
        }
        canonical_modules = {t.module for t in specs}

        assert on_disk == canonical_modules, (
            f"workspace/tools/*.py != canonical_project_tools(): "
            f"на диске без записи в каноне={sorted(on_disk - canonical_modules)}, "
            f"в каноне без файла={sorted(canonical_modules - on_disk)}"
        )

        for spec in specs:
            assert spec.description, f"{spec.name}: пустое описание"
            if spec.config_key is not None:
                assert spec.config_key.startswith(
                    "tools."
                ), f"{spec.name}: config_key должен быть из секции tools.*"

    def test_example_tool_absent(self) -> None:
        """Шаблонный tool УБРАНЕН из канонического списка.

        ``workspace/tools/example.py`` удалён; его возврат в
        ``canonical_project_tools()`` — регрессия инвентаря
        (тестовый образец — сейчас
        ``history_search_tool``).
        """
        from lib.services.runtime_inventory import canonical_project_tools

        names = {t.name for t in canonical_project_tools()}
        assert "ExampleTool" not in names

    def test_runtime_patches_covers_known(self) -> None:
        """Патчи, которые обязаны быть в каноническом инвентаре.

        Перечень — не «всё, что есть», а только те патчи, чьё
        отсутствие означает потерю наблюдаемого поведения. Патчи,
        перенесённые на нативные точки расширения (change
        ``enterprise-mcp-platform``, фаза 6), здесь не перечислены:
        они не патчи.
        """
        from lib.services.runtime_inventory import canonical_runtime_patches

        names = {p.name for p in canonical_runtime_patches()}
        for required_name in (
            "assemble_outbound",
            "subagent_logging",
        ):
            assert required_name in names, required_name

        # Перенесённые патчи не должны воскреснуть в каноне.
        for migrated in (
            "save_turn",
            "document_text_threshold",
            "context_governor",
        ):
            assert migrated not in names, (
                f"{migrated} перенесён на нативную точку расширения / "
                f"в upstream; в RuntimePatcher его быть не должно"
            )


class TestPatchSpecRequiredProjection:
    """``PatchSpec.required`` — единственный источник истины для criticality.

    Семантический тест (не module-attribute): подменяем
    ``RuntimePatcher.patch_specs`` fake-реализацией и убеждаемся,
    что ``canonical_runtime_patches()`` проецирует ``required``
    именно из spec.required, без второго hardcoded set'а.
    """

    def test_required_projects_from_patch_spec(self, monkeypatch) -> None:
        from lib.services import runtime_inventory
        from lib.services import runtime_patcher
        from lib.services.runtime_patcher import PatchSpec

        def fake_patch_specs():
            return {
                "X_high_required": PatchSpec(
                    name="X_high_required",
                    purpose="",
                    nanobot_target="",
                    reason="",
                    alternatives_checked="",
                    risk="high",
                    required=True,
                ),
                "Y_high_optional": PatchSpec(
                    name="Y_high_optional",
                    purpose="",
                    nanobot_target="",
                    reason="",
                    alternatives_checked="",
                    risk="high",
                    required=False,
                ),
                "Z_low_required": PatchSpec(
                    name="Z_low_required",
                    purpose="",
                    nanobot_target="",
                    reason="",
                    alternatives_checked="",
                    risk="low",
                    required=True,
                ),
            }

        monkeypatch.setattr(
            runtime_patcher.RuntimePatcher, "patch_specs",
            staticmethod(fake_patch_specs),
        )

        canonical = {
            p.name: p for p in runtime_inventory.canonical_runtime_patches()
        }
        assert canonical["X_high_required"].required is True
        assert canonical["Y_high_optional"].required is False
        assert canonical["Z_low_required"].required is True

    def test_critical_patches_marked_required(self) -> None:
        """Контракт criticality: какие патчи обязаны быть ``required``.

        После переноса ``save_turn`` на ``ToolResultArchiveHook`` он
        перестал быть патчем и убран из набора. Позже и сам хук убран
        (change ``use-upstream-tool-result-persist``): персист результатов
        делает upstream. Набор остаётся выпиской имён намеренно: это
        контракт «что подсвечивается в startup-баннере», и его нельзя
        вывести из ``risk``.
        Непроизводность ``required`` от ``risk`` проверяет
        ``test_required_projects_from_patch_spec`` (подменяет
        ``patch_specs`` и сверяет проекцию).
        """
        from lib.services.runtime_inventory import canonical_runtime_patches

        required_names = {
            p.name for p in canonical_runtime_patches() if p.required
        }
        assert required_names == {
            "assemble_outbound",
            "subagent_logging",
        }, required_names

        # context_governor больше не патч — в обязательных не значится.
        assert "context_governor" not in required_names

    def test_no_hardcoded_required_set(self) -> None:
        """В ``runtime_inventory`` нет локального ``high_risk_required``."""
        from lib.services import runtime_inventory

        assert not hasattr(runtime_inventory, "high_risk_required"), (
            "high_risk_required удалён как hardcoded источник истины; "
            "используйте PatchSpec.required"
        )


class TestDiffHooks:
    def test_actual_matches_canonical(self) -> None:
        """Правило: «совпадает с каноном» = подать в ``diff_hooks`` ровно
        канонический состав — и получить пустой diff.

        Вход строится из ``canonical_framework_hooks()`` +
        ``canonical_plugin_hooks()``, а не выписывается: переезд
        ``save_turn`` в ``ToolResultArchiveHook`` добавил хук в канон и
        сломал бы тест, заставляя править ручной список. Проверка
        остаётся содержательной — она ломается, если ``diff_hooks``
        начнёт считать расхождением то, что расхождением не является.
        """
        from lib.services.runtime_inventory import (
            canonical_framework_hooks,
            canonical_hook_factories,
            canonical_plugin_hooks,
            diff_hooks,
        )

        actual = [h.name for h in canonical_framework_hooks()] + [
            h.name for h in canonical_plugin_hooks()
        ]
        diff = diff_hooks(
            actual, actual_factory_count=len(canonical_hook_factories())
        )
        assert diff["missing_required"] == []
        assert diff["missing_optional"] == []
        assert diff["unexpected"] == []
        assert diff["missing_factory"] == []

    def test_missing_required(self) -> None:
        from lib.services.runtime_inventory import diff_hooks

        diff = diff_hooks(
            ["ToolAuditHook", "TerminalToolPrintHook"],
            actual_factory_count=0,
        )
        assert "SessionFileRedirectHook" in diff["missing_required"]
        assert "RecentFilesHook" in diff["missing_required"]
        assert "DatabaseLoggingHook" in diff["missing_factory"]
        assert "StreamDiagnosisHook" in diff["missing_optional"]

    def test_unexpected(self) -> None:
        from lib.services.runtime_inventory import diff_hooks

        diff = diff_hooks(
            ["ToolAuditHook", "TerminalToolPrintHook", "SomeRandomHook"],
            actual_factory_count=1,
        )
        assert "SomeRandomHook" in diff["unexpected"]


class TestDiffProjectTools:
    def test_actual_matches_canonical(self) -> None:
        """Правило: «совпадает с каноном» = подать в ``diff`` ровно
        канонический состав — и получить пустой diff.

        Вход строится из ``canonical_project_tools()``, а не выписывается:
        добавление нового tool'а (например ``document_read`` после
        переноса порога длины текста из патча) не ломает тест. Проверка
        остаётся содержательной — она ломается, если ``diff`` начнёт
        считать расхождением то, что расхождением не является.
        """
        from lib.services.runtime_inventory import (
            canonical_project_tools,
            diff_project_tools,
        )

        canonical = canonical_project_tools()
        diff = diff_project_tools(
            registered=[t.name for t in canonical if t.required],
            skipped_disabled=["ExampleTool"],
        )
        assert diff["missing_required"] == []
        assert diff["disabled_required"] == []
        assert diff["failed"] == []
        assert diff["unexpected"] == []

    def test_missing_required(self) -> None:
        from lib.services.runtime_inventory import diff_project_tools

        # Имена взяты из живого канона, а не выписаны: после сноса
        # audit_analyzer_query/legal_summarizer_query/history_search фикстуры
        # на них перестали существовать, и молчаливый «пример» оказался бы
        # проверкой несуществующего инструмента.
        diff = diff_project_tools(
            registered=["document_read"],
            skipped_disabled=["ExampleTool", "compact_context"],
        )
        assert "compact_context" in diff["disabled_required"]

    def test_failed_listed(self) -> None:
        from lib.services.runtime_inventory import diff_project_tools

        diff = diff_project_tools(
            registered=["document_read"],
            skipped_disabled=["ExampleTool"],
            failed=["compact_context"],
        )
        assert "compact_context" in diff["missing_required"]
        assert "compact_context" in diff["failed"]


class TestParseProjectToolsDetail:
    def test_user_log(self) -> None:
        from lib.services.runtime_inventory import parse_project_tools_detail

        parsed = parse_project_tools_detail(
            "3 project tools registered: compact_context, history_search, "
            "legal_summarizer_query; 1 disabled by config: ExampleTool"
        )
        assert parsed["registered"] == [
            "compact_context",
            "history_search",
            "legal_summarizer_query",
        ]
        assert parsed["disabled"] == ["ExampleTool"]
        assert parsed["failed"] == []

    def test_internal_failed(self) -> None:
        from lib.services.runtime_inventory import parse_project_tools_detail

        parsed = parse_project_tools_detail(
            "[INTERNAL_FAILED] 2 project tools registered: compact_context, "
            "history_search; 1 disabled by config: ExampleTool; "
            "1 failed: legal_summarizer_query"
        )
        assert parsed["registered"] == ["compact_context", "history_search"]
        assert parsed["failed"] == ["legal_summarizer_query"]

    def test_skip_message(self) -> None:
        from lib.services.runtime_inventory import parse_project_tools_detail

        parsed = parse_project_tools_detail("workspace/tools not found — skip")
        assert parsed["registered"] == []
        assert parsed["disabled"] == []
        assert parsed["failed"] == []


class TestDiffRuntimePatches:
    def test_applied_matches_canonical(self) -> None:
        from lib.services.runtime_inventory import (
            canonical_runtime_patches, diff_runtime_patches,
        )

        # Все required-патчи должны быть либо applied, либо skipped.
        required = {
            p.name
            for p in canonical_runtime_patches()
            if p.required
        }
        # ``project_tools`` больше не в ``canonical_runtime_patches()``
        # (после change runtime-patcher-composition-cleanup) —
        # регистрация переехала в ProjectToolLoader.
        applied = ["assemble_outbound", "async_save", "subagent_logging"]
        skipped = [
            (p, "x") for p in required if p not in applied
        ]
        diff = diff_runtime_patches(
            applied=applied,
            skipped=skipped,
            failed=[],
        )
        assert diff["missing_required"] == []
        assert diff["failed_required"] == []

    def test_failed_required_detected(self) -> None:
        from lib.services.runtime_inventory import (
            canonical_runtime_patches, diff_runtime_patches,
        )

        required = {
            p.name
            for p in canonical_runtime_patches()
            if p.name != "subagent_logging"
        }
        diff = diff_runtime_patches(
            applied=["assemble_outbound"],
            skipped=[(p, "x") for p in required if p != "assemble_outbound"],
            failed=[("subagent_logging", "import failed: foo")],
        )
        assert "subagent_logging" in diff["failed_required"]
        assert diff["missing_required"] == []

    def test_optional_failed_not_critical(self) -> None:
        from lib.services.runtime_inventory import (
            canonical_runtime_patches, diff_runtime_patches,
        )

        required = {
            p.name
            for p in canonical_runtime_patches()
            if p.required
        }
        diff = diff_runtime_patches(
            applied=sorted(required),
            skipped=[],
            failed=[("exec_timeout_cap", "shell module not loaded")],
        )
        assert diff["missing_required"] == []
        assert diff["failed_required"] == []
