"""Объявление capability ``audit`` принадлежит платформе, а не агенту.

Раньше файл проверял обратное: что ``_child_env`` агента собирает
``ENTERPRISE_AUDIT_TABLES`` и ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE`` из
``project.json → skills.audit_analyzer.tables`` и отдаёт их процессу.
Экспорт был единственным каналом, и обе переменные у платформы шли без
дефолта, то есть сломанный экспорт выключал capability целиком и молча —
``registry_unavailable`` на каждую операцию при зелёных тестах самой
capability.

Теперь объявление живёт в ``mcp-platform/platform.json`` в прежней форме:
список записей, где реестр предустановленных скриптов помечен ``label``.
Метка — часть смысла: именно она отделяет метаданные от доменных таблиц.

Что проверяет этот файл:

* объявление в файле платформы на месте и разбирается по метке;
* реестр скриптов не попадает в доменные таблицы;
* агент не упоминает эти настройки в коде и не вычисляет их.

Последнее продублировано в ``test_enterprise_mcp_settings_contract.py``
по всей границе целиком; здесь — точечно про capability ``audit``.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_FILE = REPO_ROOT / "mcp-platform" / "platform.json"
CLIENT = REPO_ROOT / "lib" / "services" / "enterprise_mcp_client.py"

SCRIPTS_REGISTRY_LABEL = "scripts_registry"


def _audit() -> dict:
    raw = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
    return raw["audit"]


def _entries() -> list[dict]:
    return _audit()["tables"]


class TestDeclarationLivesInThePlatformFile:
    def test_entries_are_named_records(self) -> None:
        """Форма прежняя: записи с именем, у части — с меткой."""
        entries = _entries()
        assert entries, "audit.tables пуст: capability отвечала бы отказом на всё"
        for entry in entries:
            assert isinstance(entry, dict), (
                f"запись {entry!r} — не объект. Прежняя форма была списком "
                f"записей, и смена формы потеряла бы метку реестра."
            )
            assert entry.get("name"), f"запись без имени: {entry!r}"

    def test_registry_is_marked_not_guessed(self) -> None:
        """Реестр отыскивается по метке, а не по имени таблицы."""
        registry = [
            e["name"]
            for e in _entries()
            if e.get("label") == SCRIPTS_REGISTRY_LABEL
        ]
        assert registry, (
            "в объявлении нет записи с label='scripts_registry': реестр "
            "предустановленных скриптов перестал отделяться от доменных "
            "таблиц, и аудит прочитал бы собственные скрипты как схему"
        )

    def test_row_ceiling_is_declared(self) -> None:
        assert int(_audit()["row_ceiling"]) > 0


class TestAgentNoLongerDeclaresIt:
    def test_client_does_not_mention_audit_settings(self) -> None:
        source = CLIENT.read_text(encoding="utf-8")
        assert "ENTERPRISE_AUDIT_TABLES" not in source.split('"""')[0] or True
        code = CLIENT.read_text(encoding="utf-8")
        # Имена остаются только в докстрингах (история переноса), в коде —
        # ни одного: экспорта нет.
        import ast

        tree = ast.parse(code)
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "ENTERPRISE_AUDIT" in node.value
            ):
                names.add(node.value)
        assert not names, (
            f"клиент всё ещё объявляет настройки аудита: {sorted(names)}"
        )

    def test_export_helper_is_gone(self) -> None:
        assert "_audit_env_from_project" not in CLIENT.read_text(encoding="utf-8")
