"""Тесты: ``NANOBOT_PROFILE`` MUST NOT читаться runtime-кодом.

Env-передача профиля удалена из новой модели (см.
``openspec/specs/configuration/profiles/spec.md`` и
``openspec/specs/runtime/entrypoints/spec.md``). Профиль — только
аргумент ``--profile`` в argv application entrypoint. Любое чтение
``os.environ.get("NANOBOT_PROFILE")`` / ``os.getenv("NANOBOT_PROFILE")``
или ``environ[...]`` в runtime-коде — регрессия.

Покрывает инвариант:
  * tests test_cli_agent_profile.py::TestCliHardcodesProfileInLifecycle
    проверяет, что ``config._initialize_settings`` игнорирует env;
  * этот файл делает статический grep по runtime-каталогам.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Каталоги runtime-кода, в которых запрещено чтение NANOBOT_PROFILE.
# НЕ входит `tests/` — там допустимо (тесты проверяют, что env ИГНОРИРУЕТСЯ).
_RUNTIME_DIRS = (
    "lib",
    "workspace",
    "tools",
    "sql",
    "benchmarks",
    "config.py",
    "gateway.py",
    "cli_agent.py",
    "streamlit_app.py",
)

# Допустимые упоминания NANOBOT_PROFILE в runtime:
#   * комментарии (начинающиеся с `#`) и docstrings — допустимы;
#   * тесты-проверки игнорирования env — НЕ в runtime (см. _RUNTIME_DIRS);
# Этот тест делает жёсткую проверку: ЛЮБОЕ упоминание (включая комментарий)
# в runtime-коде = регрессия, потому что в новой модели упоминание
# переменной как источника истины ЗАПРЕЩЕНО.
_FORBIDDEN_PATTERNS = (
    re.compile(r"NANOBOT_PROFILE"),
    re.compile(r"nanobot_profile"),
)


def _iter_runtime_files() -> list[Path]:
    out: list[Path] = []
    for entry in _RUNTIME_DIRS:
        p = _PROJECT_ROOT / entry
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            for child in p.rglob("*.py"):
                out.append(child)
    return out


def test_runtime_code_does_not_reference_nanobot_profile() -> None:
    offenders: list[str] = []
    for path in _iter_runtime_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for pat in _FORBIDDEN_PATTERNS:
                if pat.search(line):
                    offenders.append(
                        f"{path.relative_to(_PROJECT_ROOT)}:{n}: {line.strip()}"
                    )
                    break
    assert not offenders, (
        "Runtime-код НЕ ДОЛЖЕН ссылаться на NANOBOT_PROFILE — "
        "профиль передаётся только через argv --profile. Нарушения:\n"
        + "\n".join(offenders)
    )
