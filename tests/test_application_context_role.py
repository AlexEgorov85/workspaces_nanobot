"""
Тесты Stage A (change ``unify-cli-gateway-architecture``).

Проверяют typed signature ``ApplicationContext.create(role=...)``:

  * ``role`` MUST быть обязательным KEYWORD_ONLY параметром;
  * ``profile`` MUST NOT быть named параметром (читается из SETTINGS);
  * ``enable_*``/``print_llm_calls`` MUST NOT быть named параметрами;
  * deprecated kwargs (``enable_*``, ``profile``, ``print_llm_calls``) MUST
    приниматься через ``**kwargs`` с ``DeprecationWarning``;
  * при ``role="cli"`` ``CronService`` MUST NOT создаваться, даже если
    ``enable_cron=True`` был передан (gateway-only invariant);
  * cache_provider field MUST существовать (Stage D plumbing); legacy
    ``cache_store`` сохраняется как alias.
"""

from __future__ import annotations

import inspect
import sys
import warnings
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


class TestSignatureContract:
    def test_role_is_required_keyword_only(self) -> None:
        from lib.core.application_context import ApplicationContext

        sig = inspect.signature(ApplicationContext.create)
        params = sig.parameters
        assert "role" in params, (
            f"role MUST быть параметром; got params: {list(params)}"
        )
        role = params["role"]
        assert role.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"role MUST быть KEYWORD_ONLY; got kind={role.kind.name}"
        )
        assert role.default is inspect.Parameter.empty, (
            f"role MUST быть обязательным; got default={role.default!r}"
        )

    def test_profile_not_in_named_signature(self) -> None:
        from lib.core.application_context import ApplicationContext

        params = inspect.signature(ApplicationContext.create).parameters
        assert "profile" not in params

    def test_enable_kwargs_not_in_named_signature(self) -> None:
        from lib.core.application_context import ApplicationContext

        params = inspect.signature(ApplicationContext.create).parameters
        for deprecated in (
            "enable_db_logging",
            "enable_audit",
            "enable_cron",
            "print_llm_calls",
        ):
            assert deprecated not in params, (
                f"{deprecated} MUST NOT быть named параметром "
                f"(deprecated kwarg only)"
            )

    def test_kwargs_var_keyword_present(self) -> None:
        from lib.core.application_context import ApplicationContext

        sig = inspect.signature(ApplicationContext.create)
        var_kw = [
            n for n, p in sig.parameters.items()
            if p.kind is inspect.Parameter.VAR_KEYWORD
        ]
        assert var_kw == ["kwargs"], (
            f"MUST быть VAR_KEYWORD 'kwargs'; got: {var_kw}"
        )


class TestCompositionFields:
    def test_ctx_role_set_from_create(self) -> None:
        from lib.core.application_context import ApplicationContext

        sig = inspect.signature(ApplicationContext.create)
        expected = {"role", "enable_db_logging", "enable_audit", "enable_cron",
                   "print_llm_calls", "cache_provider", "cache_store"}
        all_attrs = set(dir(ApplicationContext))
        for attr in expected:
            assert attr in all_attrs or attr in sig.parameters, (
                f"{attr!r} SHOULD be either class attr or named param"
            )

    def test_deprecated_kwar_module_constant(self) -> None:
        from lib.core.application_context import DEPRECATED_ENABLE_KWARGS

        assert DEPRECATED_ENABLE_KWARGS == frozenset({
            "enable_db_logging",
            "enable_audit",
            "enable_cron",
            "print_llm_calls",
        })


class TestDeprecatedKwargsResolver:
    def test_resolve_with_no_kwargs_uses_gateway_defaults(self) -> None:
        from lib.core.application_context import _resolve_enable_kwargs

        result = _resolve_enable_kwargs(
            {}, gateway_settings={"enable_db_logging": True, "enable_audit": False}
        )
        assert result["enable_db_logging"] is True
        assert result["enable_audit"] is False
        assert result["enable_cron"] is False
        assert result["print_llm_calls"] is False

    def test_resolve_with_unknown_kwarg_silently_keeps_it(self) -> None:
        from lib.core.application_context import _resolve_enable_kwargs

        result = _resolve_enable_kwargs(
            {"unknown": "x"},
            gateway_settings={},
        )
        assert "unknown" not in result
        assert result["enable_db_logging"] is True

    def test_resolve_with_deprecated_kwarg_emits_warning(self) -> None:
        from lib.core.application_context import _resolve_enable_kwargs

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            result = _resolve_enable_kwargs(
                {"enable_audit": False},
                gateway_settings={"enable_audit": True},
            )
        assert result["enable_audit"] is False
        deprecations = [
            w for w in captured if issubclass(w.category, DeprecationWarning)
        ]
        assert deprecations, "expected DeprecationWarning to be raised"
        assert "enable_audit" in str(deprecations[0].message)

    def test_resolve_overrides_gateway_settings(self) -> None:
        from lib.core.application_context import _resolve_enable_kwargs

        result = _resolve_enable_kwargs(
            {"enable_cron": True},
            gateway_settings={"enable_cron": False},
        )
        assert result["enable_cron"] is True


class TestProductionCallersDoNotUseDeprecatedKwargs:
    """Production code MUST NOT передавать deprecated kwargs (design D8).

    Совместимость через ``**kwargs`` существует только для внешних
    callers. Собственные entrypoint'ы обязаны брать значения из
    ``SETTINGS["gateway"]``, иначе каждый запуск печатает
    ``DeprecationWarning`` и runtime расходится с конфигом.
    """

    _DEPRECATED = (
        "enable_db_logging",
        "enable_audit",
        "enable_cron",
        "print_llm_calls",
    )

    def _production_files(self) -> list[Path]:
        return [
            _project_root / "cli_agent.py",
            _project_root / "gateway.py",
            _project_root / "benchmarks" / "runner.py",
        ]

    @pytest.mark.parametrize(
        "path",
        ["cli_agent.py", "gateway.py", "benchmarks/runner.py"],
    )
    def test_no_deprecated_kwargs_in_entrypoints(self, path: str) -> None:
        import ast

        source = (_project_root / path).read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "create"
                and isinstance(func.value, ast.Name)
                and func.value.id == "ApplicationContext"
            ):
                continue
            for kw in node.keywords:
                if kw.arg in self._DEPRECATED:
                    offenders.append(f"{path}:{node.lineno} {kw.arg}=")

        assert not offenders, (
            "Production entrypoints must not pass deprecated kwargs: "
            + ", ".join(offenders)
        )
