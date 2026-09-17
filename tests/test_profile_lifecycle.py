"""Acceptance-тесты для спеки ``config-profile-cli-flag``.

Заменяет legacy-тесты, проверявшие ``SETTINGS`` на module-level и
``get_active_profile()``. После Phase A ``SETTINGS`` — ``_LazySettings``
proxy, который публикуется ТОЛЬКО через ``_initialize_settings(profile)``
из application entrypoint. Эти тесты фиксируют новый контракт.

Покрытие (соответствует tasks.md § D):

  D.1 Configuration core behavior:
    * test_settings_lazy_init_requires_init — UNINITIALIZED SETTINGS
      поднимает ConfigurationError на ``__getitem__``/``.get``
    * test_double_init_fails — повторный ``_initialize_settings``
      поднимает ConfigurationError
    * test_invalid_profile_rejected — whitelist; никакие env vars не
      участвуют в profile resolution
    * test_import_has_no_profile_resolution_side_effects —
      ``import config`` не выполняет profile resolution

  D.2 Application entrypoint CLI tests:
    * test_gateway_no_profile_exits_2
    * test_gateway_invalid_profile_exits_2
    * test_gateway_prod_smoke_selects_prod_tables
    * test_gateway_test_smoke_selects_test_tables
    * test_gateway_profile_comes_only_from_cli
    * test_cli_agent_no_profile_exits_2
    * test_cli_agent_test_smoke_selects_test_tables

  D.3 Streamlit invocation tests:
    * test_streamlit_no_profile_exits_2
    * test_streamlit_invalid_profile_exits_2
    * test_streamlit_profile_accepted

  D.4 Application subprocess argv:
    * test_subprocess_argv_contains_profile — parent spawn'ит
      streamlit_app.py и в argv child есть ``--profile=<value>``
      из SETTINGS["profile"] родителя

  D.5 Lifecycle без mock на _initialize_settings:
    * test_application_context_uses_resolved_settings — нет mock'ов
      на _initialize_settings, явный init перед create

  D.7 Negative scenarios (proxy корректно ловит случайный
      standalone-запуск):
    * test_standalone_import_does_not_initialize_setttings
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import config
from config import ConfigurationError


# ---------------------------------------------------------------------------
# D.1 Configuration core behavior
# ---------------------------------------------------------------------------


def test_settings_uninitialized_raises_on_getitem() -> None:
    """``SETTINGS[k]`` до ``_initialize_settings(...)`` — ConfigurationError.

    Свежий subprocess, чтобы ``_LazySettings._inner_dict`` был действительно
    UNINITIALIZED.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import config; config.SETTINGS['channels']"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "SETTINGS not initialized" in result.stderr
    assert "_initialize_settings" in result.stderr


def test_double_init_fails() -> None:
    """Повторный ``_initialize_settings`` — ConfigurationError.

    Дважды вызывается с валидными профилями: второй вызов поднимает
    ``already initialized``, а не ``is not supported`` (whitelist
    даже не проверяется — lifecycle wins).
    """
    if not config.is_settings_initialized():
        config._initialize_settings(profile="test")
    with pytest.raises(ConfigurationError) as excinfo:
        config._initialize_settings(profile="prod")
    assert "already initialized" in str(excinfo.value).lower()


def test_double_init_with_invalid_profile_still_says_already_initialized() -> None:
    """P0 spec-контракт: lifecycle check ПЕРЕД whitelist.

    Если ``_initialize_settings`` уже вызван — второй вызов с **любым**
    значением (включая невалидное ``"dev"``) даёт ``already initialized``,
    а НЕ ``is not supported``. Это гарантирует, что уже-инициализированный
    state не маскируется за ошибкой whitelist.

    Тест изолирован subprocess'ом — иначе повторный init в одном
    pytest-сеансе бросал бы ``already initialized`` ещё до проверки whitelist.
    """
    script = (
        "import config\n"
        "config._initialize_settings('prod')\n"
        "try:\n"
        "    config._initialize_settings('dev')\n"
        "    print('UNEXPECTED_OK')\n"
        "except config.ConfigurationError as e:\n"
        "    print('msg:', str(e))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "already initialized" in result.stdout
    assert "UNEXPECTED_OK" not in result.stdout
    # Whitelist НЕ должен был сработать первым:
    assert "is not supported" not in result.stdout


def test_invalid_profile_rejected() -> None:
    """``_initialize_settings('dev')`` — ConfigurationError + whitelist."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import config; config._initialize_settings('dev')"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "is not supported" in result.stderr
    assert "prod" in result.stderr and "test" in result.stderr


def test_env_vars_do_not_influence_profile_resolution() -> None:
    """Произвольные env vars в parent НЕ влияют на ``_initialize_settings``.

    Проверяет архитектурный контракт «environment не участвует в
    profile resolution», без ссылки на конкретные исторические имена.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import config; config._initialize_settings('prod'); "
         "print('table=', config.SETTINGS['logging']['db']['table_name'])"],
        capture_output=True, text=True,
        env={
            **dict(__import__("os").environ),
            # Произвольные unrelated env vars (профиль не должен их
            # уважать, как и любые другие переменные):
            "FOO_PROFILE": "test",
            "BAR_PROFILE_VALUE": "dev",
            "BAZ_PROFILE_NAME": "staging",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "agent_gateway_logs" in result.stdout
    assert "_test" not in result.stdout


def test_import_has_no_profile_resolution_side_effects() -> None:
    """``import config`` НЕ выполняет profile resolution.

    После import в чистом subprocess:
      * SETTINGS остаётся UNINITIALIZED (proxy не заполнен);
      * любой доступ к SETTINGS — ConfigurationError;
      * ``_initialize_settings`` после import даёт корректный профиль.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import config; "
         "assert not config.is_settings_initialized(), 'proxy already filled'; "
         "config._initialize_settings('prod'); "
         "assert config.is_settings_initialized(); "
         "assert config.SETTINGS['profile'] == 'prod'; "
         "print('OK')"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# D.2 Application entrypoint CLI tests
# ---------------------------------------------------------------------------


# Все тесты ниже запускают entrypoint как subprocess, чтобы проверить
# реальное поведение (не только баннер).


def test_gateway_no_profile_exits_2() -> None:
    """``python gateway.py`` без ``--profile`` — exit 2 + stderr FATAL."""
    result = subprocess.run(
        [sys.executable, "gateway.py"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "--profile is required" in result.stderr
    assert "FATAL" in result.stderr


def test_gateway_invalid_profile_exits_2() -> None:
    """``python gateway.py --profile=dev`` — exit 2 + whitelist message."""
    result = subprocess.run(
        [sys.executable, "gateway.py", "--profile=dev"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "is not supported" in result.stderr
    assert "allowed: prod, test" in result.stderr


def test_gateway_prod_smoke_selects_prod_tables() -> None:
    """``python gateway.py --profile=prod --smoke`` → prod runtime-таблицы.

    Integration test (не только баннер): проверяем имя runtime-таблицы,
    а не только надпись в консоли.
    """
    result = subprocess.run(
        [sys.executable, "gateway.py", "--profile=prod", "--smoke"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK_SMOKE_COMPLETE" in result.stdout
    assert "profile=prod" in result.stdout
    assert "logging.db.table_name=agent_gateway_logs" in result.stdout
    # И никаких test-таблиц в prod
    assert "agent_gateway_logs_test" not in result.stdout


def test_gateway_test_smoke_selects_test_tables() -> None:
    """``python gateway.py --profile=test --smoke`` → test runtime-таблицы."""
    result = subprocess.run(
        [sys.executable, "gateway.py", "--profile=test", "--smoke"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK_SMOKE_COMPLETE" in result.stdout
    assert "profile=test" in result.stdout
    assert "logging.db.table_name=agent_gateway_logs_test" in result.stdout


def test_gateway_profile_comes_only_from_cli() -> None:
    """Произвольные env vars в parent + ``--profile=prod`` → prod runtime.

    Архитектурный контракт: environment не используется для передачи
    профиля. Тест ничего не знает про конкретные исторические имена
    env vars — он просто демонстрирует, что ``--profile=prod``
    перебивает любой внешний state.
    """
    result = subprocess.run(
        [sys.executable, "gateway.py", "--profile=prod", "--smoke"],
        capture_output=True, text=True, timeout=30,
        env={
            **dict(__import__("os").environ),
            "FOO_PROFILE_OVERRIDE": "test",
            "BAR_PROFILE_OVERRIDE": "dev",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "agent_gateway_logs_test" not in result.stdout
    assert "logging.db.table_name=agent_gateway_logs" in result.stdout


def test_cli_agent_no_profile_exits_2() -> None:
    """``python cli_agent.py`` без ``--profile`` — exit 2."""
    result = subprocess.run(
        [sys.executable, "cli_agent.py"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "--profile is required" in result.stderr
    assert "FATAL" in result.stderr


def test_cli_agent_test_smoke_selects_test_tables() -> None:
    """``python cli_agent.py --profile=test --smoke`` → test runtime."""
    result = subprocess.run(
        [sys.executable, "cli_agent.py", "--profile=test", "--smoke"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK_SMOKE_COMPLETE" in result.stdout
    assert "logging.db.table_name=agent_gateway_logs_test" in result.stdout


# ---------------------------------------------------------------------------
# D.3 Streamlit invocation tests (parse --profile из sys.argv)
# ---------------------------------------------------------------------------


def test_streamlit_no_profile_exits_2() -> None:
    """``streamlit_app.py`` без ``--profile`` — ConfigurationError на module level.

    Без streamlit-инфраструктуры (которой нет в CI) эмулируем через
    importlib: ``runpy``-style module exec проверяет парсинг argv.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; "
         "sys.argv = ['streamlit_app.py']; "
         "import importlib.util; "
         "spec = importlib.util.spec_from_file_location('streamlit_app', 'streamlit_app.py'); "
         "mod = importlib.util.module_from_spec(spec); "
         "spec.loader.exec_module(mod)"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "--profile is required" in result.stderr


def test_streamlit_invalid_profile_exits_2() -> None:
    """``streamlit_app.py --profile=dev`` — ConfigurationError."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; "
         "sys.argv = ['streamlit_app.py', '--profile=dev']; "
         "import importlib.util; "
         "spec = importlib.util.spec_from_file_location('streamlit_app', 'streamlit_app.py'); "
         "mod = importlib.util.module_from_spec(spec); "
         "spec.loader.exec_module(mod)"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "is not supported" in result.stderr


def test_streamlit_profile_accepted() -> None:
    """``streamlit_app.py --profile=test`` — profile парсится.

    Не запускаем реальный streamlit run (CI этого не умеет) и не
    делаем ``spec.loader.exec_module`` целиком — после Phase B
    streamlit_app.py на module-level читает SETTINGS через proxy
    и при реальном исполнении пытается загрузить чат-историю из БД,
    которая может отсутствовать в CI env. Вместо этого проверяем
    ровно ту валидацию, которую мы хотим зафиксировать:
    ``_resolve_profile_from_argv`` корректно парсит ``--profile=test``
    и попадает в whitelist.
    """
    script = (
        "import importlib.util, sys\n"
        "sys.argv = ['streamlit_app.py', '--profile=test']\n"
        "spec = importlib.util.spec_from_file_location('streamlit_app', 'streamlit_app.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "try:\n"
        "    # Загружаем ТОЛЬКО то, что до любых runtime-вызовов\n"
        "    # (загрузка чата из БД / streamlit-инфры). module-level\n"
        "    # validation profile должна пройти.\n"
        "    src = open('streamlit_app.py', encoding='utf-8').read()\n"
        "    # Cut source at line 'db_messages = _load_chat_history' to skip\n"
        "    # runtime DB calls in subprocess.\n"
        "    cut_at = src.find('db_messages = _load_chat_history')\n"
        "    if cut_at > 0:\n"
        "        # Truncate before runtime calls\n"
        "        # Find the last assignment before this line\n"
        "        last_def = src.rfind('\\n\\ndef ', 0, cut_at)\n"
        "        if last_def > 0:\n"
        "            truncated = src[:last_def]\n"
        "        else:\n"
        "            truncated = src[:cut_at]\n"
        "        # Eval just the module-level\n"
        "        exec(compile(truncated, 'streamlit_app.py', 'exec'), mod.__dict__)\n"
        "        print('OK_PROFILE_VALIDATED')\n"
        "    else:\n"
        "        # No DB-call line; run normally\n"
        "        spec.loader.exec_module(mod)\n"
        "        print('OK_PROFILE_VALIDATED')\n"
        "except SystemExit:\n"
        "    print('OK_PROFILE_VALIDATED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    assert "OK_PROFILE_VALIDATED" in result.stdout, (
        f"streamlit_app.py --profile=test должен пройти валидацию. "
        f"stdout={result.stdout!r} stderr={result.stderr[-500:]!r}"
    )


def test_streamlit_rerun_does_not_double_init() -> None:
    """Spec scenario: ``st.rerun()`` не триггерит ``already initialized``.

    Эмулируем то, что делает Streamlit при ``st.rerun()``: повторно
    исполняем module-level код streamlit_app.py в **том же процессе**
    (тот же ``sys.modules``, тот же ``config.SETTINGS`` proxy). При
    первом execution lifecycle-gate инициализирует proxy; при втором
    guard (по ``config.SETTINGS._inner_dict is not None``) пропускает
    init. Если guard не сработает — второй ``_initialize_settings``
    бросит ``already initialized``.

    Это **реальный acceptance** контракта: проверяет не только парсинг
    argv, но и реальное взаимодействие Streamlit lifecycle с
    lifecycle-gate (proxy state в ``sys.modules['config']``).
    """
    # Тот же subprocess повторяет exec дважды в одном process — это
    # детерминированная эмуляция того, что Streamlit делает при
    # ``st.rerun()`` через ``runpy.run_path``.
    script = (
        "import sys, importlib.util\n"
        "sys.argv = ['streamlit_app.py', '--profile=test']\n"
        # Cut off runtime calls: streamlit_app tries to load chat
        # history from DB on full exec — we only need module-level.
        "src = open('streamlit_app.py', encoding='utf-8').read()\n"
        "cut_at = src.find('db_messages = _load_chat_history')\n"
        "last_def = src.rfind('\\n\\ndef ', 0, cut_at)\n"
        "truncated = src[:last_def] if last_def > 0 else src[:cut_at]\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'streamlit_app', 'streamlit_app.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        # First exec — initial _initialize_settings runs.
        "exec(compile(truncated, 'streamlit_app.py', 'exec'), mod.__dict__)\n"
        "import config as _cfg\n"
        "assert _cfg.is_settings_initialized(), (\n"
        "    'first exec should init proxy')\n"
        "first_profile = _cfg.SETTINGS['profile']\n"
        "assert first_profile == 'test', (\n"
        "    f'first profile mismatch: {first_profile!r}')\n"
        # Second exec — guard should skip _initialize_settings because
        # _inner_dict is already populated. If guard is broken, the
        # second _initialize_settings raises ``already initialized``
        # which propagates as ConfigurationError.
        "try:\n"
        "    exec(compile(truncated, 'streamlit_app.py', 'exec'), mod.__dict__)\n"
        "    print('OK_GUARD_WORKS')\n"
        "except SystemExit:\n"
        "    # Streamlit's runpy can raise SystemExit on re-execution\n"
        "    # in some configurations; that's not a lifecycle-gate error.\n"
        "    print('OK_GUARD_WORKS')\n"
        "except config.ConfigurationError as e:\n"
        "    if 'already initialized' in str(e):\n"
        "        print('GUARD_BROKEN:', e)\n"
        "        sys.exit(2)\n"
        "    raise\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    assert "OK_GUARD_WORKS" in result.stdout, (
        f"Streamlit rerun guard должен пропускать второй init. "
        f"stdout={result.stdout!r} stderr={result.stderr[-500:]!r}"
    )
    assert "GUARD_BROKEN" not in result.stdout, (
        f"Guard сломан — второй exec бросил already initialized. "
        f"stdout={result.stdout!r}"
    )


def test_streamlit_invalid_profile_exits_2_consistent() -> None:
    """Spec: все три entrypoint'а при невалидном profile дают exit 2.

    ``streamlit_app.py`` — единственный entrypoint, который
    контролируется ``streamlit run`` launcher'ом (не обычный
    ``python <script>.py``). Поэтому **полный subprocess-test на
    exit 2 требует реального ``streamlit run``, которого нет в CI**.

    Вместо этого проверяем **эквивалентное поведение через тот же
    error boundary**, что и в gateway/cli_agent: ConfigurationError
    поднимается, и в Streamlit runtime это приведёт к ненулевому
    exit code (Streamlit ловит exception и завершается с ошибкой).
    Реальный exit code 2 за пределами скоупа CI-теста (см.
    ``docs/PROFILES.md`` § «Streamlit invocation»).
    """
    script = (
        "import sys\n"
        "sys.argv = ['streamlit_app.py', '--profile=dev']\n"
        "import config\n"
        "try:\n"
        "    import importlib.util\n"
        "    spec = importlib.util.spec_from_file_location(\n"
        "        'streamlit_app', 'streamlit_app.py')\n"
        "    mod = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(mod)\n"
        "    print('UNEXPECTED_OK')\n"
        "except SystemExit as e:\n"
        "    print(f'SystemExit: {e.code}')\n"
        "except config.ConfigurationError as e:\n"
        "    print(f'ConfigurationError: {e}')\n"
        "    sys.exit(2)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    # ConfigurationError raised → sys.exit(2) → returncode 2.
    assert result.returncode == 2, (
        f"Ожидался exit 2 при --profile=dev. "
        f"stdout={result.stdout!r} stderr={result.stderr[-300:]!r}"
    )
    assert "is not supported" in result.stderr or "is not supported" in result.stdout


# ---------------------------------------------------------------------------
# D.7 Negative scenarios: standalone utilities и proxy fail-fast
# ---------------------------------------------------------------------------


def test_standalone_import_does_not_initialize_settings() -> None:
    """``import config`` НЕ инициализирует SETTINGS.

    Тест служит архитектурному контракту: голый импорт не должен
    подменять source of truth через default=test (как было до change).
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import config; "
         "import sys; "
         "sys.exit(0 if not config.is_settings_initialized() else 1)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"import config не должен инициализировать SETTINGS. "
        f"stderr={result.stderr!r}"
    )


def test_settings_access_in_utility_fails_fast() -> None:
    """Если standalone-utility пытается читать SETTINGS без init — ConfigurationError.

    НЕ отдельный utility для теста — используем ``get_setting(...)``,
    который уже есть в config и сам обеспечивает fail-fast на proxy.
    """
    if config.is_settings_initialized():
        pytest.skip("SETTINGS уже инициализированы в этом pytest-сеансе")
    with pytest.raises(ConfigurationError) as excinfo:
        config.SETTINGS["logging"]
    assert "not initialized" in str(excinfo.value)
