"""Страж: писатель и читатель состояния операции берут ОДИН корень.

Что ломалось. У ``query_operation`` (читатель) и у ``cli.py`` (писатель) был
один и тот же параметр ``workspace_root`` с двумя разными значениями:

* capability отдавала сервису ``self._cache_root`` — **объявленный владельцем**
  корень кэша (``ENTERPRISE_LEGAL_CACHE_ROOT`` / ``legal_summarizer.cache_root``);
* ``cli.py`` передавал ``_PLATFORM_ROOT`` — **корень платформы**.

Дальше ``manifest_root`` поверх обоих склеивал суффикс эпохи агента
``workspace/data_store/cache/skills/legal_summarizer``, которого после переноса
домена в платформу не существует ни у кого. Итог: прогон, если бы он был
запущен, писал manifest в ``<корень платформы>/workspace/data_store/...``, а
follow-up искал его в ``<корень кэша>/workspace/data_store/...``. Цепочка
«суммаризовали документ → спросили про него» не работала никогда, и не
работала бы ни разу, даже если бы кто-то запустил прогон вручную.

Почему это не ловили тесты. Юнит-тесты домена передают ``workspace_root=tmp_path``
явно и проверяют пути, собранные теми же функциями, — то есть проверяют
согласованность с собой. Ни один тест не сравнивал корень, который получит
писатель, с корнем, который получит читатель. Расхождение жило между двумя
слоями, у каждого из которых был свой тест.

Что проверяется:

* ``manifest_root`` не склеивает путь ни с чем: под объявленным корнем лежит
  ровно ``operations/`` (и это совпадает с докстрингом самого модуля);
* при одном и том же объявлении корень, который использует capability, и
  корень, который использует CLI, совпадают;
* ``cli.py`` не может вернуться к константе модуля: проверяется AST, а не
  текст, поэтому переименование имён не отключает страж, а вот возврат
  ``_PLATFORM_ROOT`` включает.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from libs.legal_summarizer.cache import manifest

#: Корень домена. Файл лежит в ``<platform>/tests/legal_summarizer/architecture/``.
DOMAIN_ROOT = Path(__file__).resolve().parents[3] / "libs" / "legal_summarizer"
CLI_PATH = DOMAIN_ROOT / "cli.py"


@pytest.fixture(autouse=True)
def _isolate_domain_config():
    """Вернуть глобальную конфигурацию домена после теста.

    Конструктор capability-сервиса с объявленным ``cache_root`` вызывает
    ``_apply_domain_config`` и подменяет конфигурацию всего домена на время
    процесса. Без восстановления ``tmp_path`` этого теста остался бы в модуле
    и уехал в следующий тест — утечка состояния через общий модуль.
    """
    from libs.legal_summarizer.llm import config as domain_config

    saved = domain_config.current()
    try:
        yield
    finally:
        domain_config.configure(saved)


def test_manifest_root_is_root_plus_operations(tmp_path: Path) -> None:
    """Под корнем кэша — ровно ``operations/``, без склейки с путём агента."""
    assert manifest.manifest_root(tmp_path) == tmp_path / "operations"


def test_manifest_root_has_no_agent_era_suffix(tmp_path: Path) -> None:
    """Ни ``workspace``, ни ``data_store`` в раскладкеoperation-level быть не должно.

    Проверка именно на компонентах пути, а не на строке суффикса: суффикс
    может быть переписан в любом виде, суть проверки — в том, что путь не
    уводит состояние в дерево чужого (уже не существующего) репозитория.
    """
    parts = manifest.manifest_root(tmp_path).parts
    assert "workspace" not in parts
    assert "data_store" not in parts


def test_writer_and_reader_roots_agree(tmp_path: Path) -> None:
    """Корень писателя и корень читателя совпадают при одном объявлении.

    Читатель — ``query_operation``, которому capability отдаёт
    ``self._cache_root``. Писатель — ``cli.py``, который обязан взять тот же
    корень. Здесь capability поднимается с объявленным ``tmp_path``, после
    чего оба корня сравниваются с путём, который получит каждый.
    """
    from servers.enterprise.capabilities.legal_summarizer.service.main import (
        LegalSummarizerService,
    )

    service = LegalSummarizerService(
        config={"legal_summarizer": {"cache_root": str(tmp_path)}}
    )

    # Что получит читатель: сервис отдаёт query_operation свой корень как есть.
    reader_root = manifest.manifest_root(service._cache_root)
    # Что получит писатель: CLI зовёт ту же функцию, что и этот тест.
    writer_root = manifest.manifest_root(manifest.skill_repo_root())

    assert reader_root == writer_root == tmp_path / "operations"


def _workspace_root_expression(path: Path) -> str | None:
    """Исходный текст значения по ключу ``workspace_root`` в литерале словаря.

    Ищем по AST, а не регуляркой: нужен именно узел присваивания, иначе
    комментарий или строка в докстринге с тем же именем выдали бы ложное
    срабатывание (ровно такой ложный вывод и стоил хранения состояния в чужом
    дереве).
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "workspace_root":
                return ast.unparse(value)
    return None


def test_cli_writer_uses_declared_root_not_module_constant() -> None:
    """CLI-писатель зовёт общий резолвер, а не константу модуля.

    Это та самая асикция, из-за которой состояние писалось в один каталог, а
    читалось из другого. Возврат ``_PLATFORM_ROOT`` должен ронять этот страж.
    """
    expression = _workspace_root_expression(CLI_PATH)
    assert expression == "manifest.skill_repo_root()", (
        "cli.py обязан передавать в качестве workspace_root общий корень "
        f"состояния (manifest.skill_repo_root()), а передаёт {expression!r}. "
        "Любая константа модуля означает отдельный путь для писателя и "
        "разрыв цепочки «прогон → follow-up»."
    )


def test_manifest_root_matches_module_docstring_layout(tmp_path: Path) -> None:
    """Раскладка, которую объявляет докстринг модуля, и раскладка, что в коде.

    Докстринг ``cache/manifest.py`` обещает ``operations/<operation_id>/...``.
    Расхождение с ним и было исходной поломкой: реализация склеивала путь
    эпохи агента, о котором докстринг не говорил уже ничего.
    """
    docstring = (DOMAIN_ROOT / "cache" / "manifest.py").read_text(encoding="utf-8-sig")
    assert "operations/<operation_id>/manifest.json" in docstring
    assert manifest.manifest_root(tmp_path) == tmp_path / manifest.OPERATIONS_DIRNAME
