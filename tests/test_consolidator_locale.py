"""Тесты ``lib/services/consolidator_locale.py``.

Проверяем monkeypatch Jinja2-loader'а: шаблоны из каталога переопределений
имеют приоритет над штатными ``nanobot/templates/``, повторный вызов
идемпотентен, у несуществующего каталога — no-op.
"""

import pytest
from jinja2 import ChoiceLoader, FileSystemLoader

from lib.services.consolidator_locale import (
    _overrides_dir,
    apply_template_overrides,
)

_TEMPLATE = "agent/consolidator_archive.md"


@pytest.fixture
def env():
    from nanobot.utils.prompt_templates import _environment

    env = _environment()
    original_loader = env.loader
    yield env
    env.loader = original_loader


@pytest.fixture
def overrides_dir(tmp_path):
    d = tmp_path / "overrides"
    (d / "agent").mkdir(parents=True)
    (d / "agent" / "consolidator_archive.md").write_text(
        "CUSTOM OVERRIDE", encoding="utf-8"
    )
    return d


def test_render_prefers_override(env, overrides_dir):
    from nanobot.utils.prompt_templates import render_template

    assert apply_template_overrides(overrides_dir) is True
    assert isinstance(env.loader, ChoiceLoader)
    assert render_template(_TEMPLATE, strip=True) == "CUSTOM OVERRIDE"


def test_render_falls_back_to_stock_when_no_override(env, tmp_path):
    """Пустой (без нужного файла) каталог не ломает остальные шаблоны."""
    from nanobot.utils.prompt_templates import render_template

    d = tmp_path / "overrides"
    d.mkdir()
    assert apply_template_overrides(d) is True
    # Файла нет в overrides — берётся штатный шаблон nanobot (не пусто).
    stock = render_template(_TEMPLATE, strip=True)
    assert stock and "SNIP" in stock


def test_apply_is_idempotent(env, overrides_dir):
    apply_template_overrides(overrides_dir)
    apply_template_overrides(overrides_dir)

    assert isinstance(env.loader, ChoiceLoader)
    searchpaths = [
        str(p)
        for loader_ in env.loader.loaders
        if isinstance(loader_, FileSystemLoader)
        for p in loader_.searchpath
    ]
    assert searchpaths.count(str(overrides_dir.resolve())) == 1


def test_missing_dir_is_noop(env, tmp_path):
    assert apply_template_overrides(tmp_path / "nope") is False
    assert not isinstance(env.loader, ChoiceLoader)


def test_default_overrides_dir_points_to_workspace():
    p = _overrides_dir()
    assert p.name == "overrides"
    assert p.parent.name == "workspace"
    assert p.exists()