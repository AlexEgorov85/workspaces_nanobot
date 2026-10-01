"""Перенос объявления таблиц аудита в ``platform.json`` — чем он защищён.

Дефект, который ловит этот файл
--------------------------------

Таблицы аудита и реестр предустановленных скриптов объявлялись агентом
(``project.json → skills.audit_analyzer.tables``) и передавались процессу
переменными ``ENTERPRISE_AUDIT_TABLES`` / ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE``.
Формально MCP читал настройки агента, а по факту знание о проекте лежало в
чужом окружении: сломанный экспорт выключал capability ``audit`` целиком и
молча (``registry_unavailable`` на каждую операцию).

Теперь это платформенное значение в файле. Проверяем ровно то свойство,
ради которого перенос состоялся: **файл — источник, а не декорация**, и
форма объявления не поехала.

Форма прежняя — список записей, где реестр помечен ``label``:

* значение приходит из файла, а не из окружения процесса;
* окружение по-прежнему перебивает файл (развёртывание может задать руками),
  но тогда источник виден как ``env:*`` — молчаливого дубля не остаётся;
* отсутствующий или пустой список останавливает сервер с именем ключа, а не
  превращается в пустой белый список;
* **метка решает**: запись с ``label="scripts_registry"`` возвращается
  отдельно и в доменные таблицы не попадает — аудит не должен читать
  собственные предустановленные скрипты в обход проверки строк;
* объявление без метки (значение из окружения) реестром не считается:
  capability отвечает ``registry_unavailable`` вместо того, чтобы молча
  выдать метаданные за доменную таблицу.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.settings import (
    BY_NAME,
    FROM_FILE,
    OWNER_PLATFORM,
    PLATFORM_CONFIG_PATH,
    SCRIPTS_REGISTRY_LABEL,
    Settings,
)
from servers.enterprise.server import _audit_config

from conftest import DUMMY_SECRETS

TABLES_KEY = "audit.tables"


def _settings(**overrides: str) -> Settings:
    """Настройки как на процессе: файл настоящий, окружение чистое."""
    env = dict(DUMMY_SECRETS)
    env.update(overrides)
    return Settings(env=env, secrets={})


def _config_with(tmp_path: Path, mutate) -> Path:
    """Копия platform.json с правкой — чтобы не трогать рабочий файл."""
    raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    target = tmp_path / "platform.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


def _file_entries() -> list[dict]:
    raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
    return raw["audit"]["tables"]


class TestTheFileIsTheOwner:
    def test_registry_says_the_file_is_the_owner(self) -> None:
        setting = BY_NAME["ENTERPRISE_AUDIT_TABLES"]
        assert setting.owner == OWNER_PLATFORM, (
            f"владелец {setting.owner!r} — значит, значение снова придёт "
            f"из окружения агента"
        )
        assert setting.default is FROM_FILE, (
            f"в коде есть значение {setting.default!r}; пока оно не FROM_FILE, "
            f"ключ в файле можно забыть, и сервер поднимется"
        )
        assert setting.key == TABLES_KEY

    def test_registry_table_is_no_longer_a_separate_setting(self) -> None:
        """Реестр выводится из метки, а не живёт вторым ключом.

        Второй ключ — это вторая правка: метка ``scripts_registry`` объясняла,
        почему реестр не доменная таблица, и отдельный настройкой это
        объяснение заменялось.
        """
        assert "ENTERPRISE_SCRIPTS_REGISTRY_TABLE" not in BY_NAME

    def test_declaration_comes_from_the_file_verbatim(self) -> None:
        settings = _settings()
        declared = settings.get("ENTERPRISE_AUDIT_TABLES")
        assert settings.source("ENTERPRISE_AUDIT_TABLES") == "file:platform.json"
        assert declared == tuple(
            (entry["name"], entry.get("label", "")) for entry in _file_entries()
        ), "объявление разобралось не так, как записано в файле"

    def test_capability_config_splits_by_label(self) -> None:
        entries = _file_entries()
        config = _audit_config(_settings())
        assert config["scripts_registry"]["table"] == next(
            (e["name"] for e in entries if e.get("label") == SCRIPTS_REGISTRY_LABEL),
            "",
        ), "запись с меткой не нашлась: реестр скриптов молча выпал"
        assert config["audit"]["tables"] == [
            e["name"] for e in entries if e.get("label") != SCRIPTS_REGISTRY_LABEL
        ], "разбор по метке разошёлся с объявлением"
        # Проверки выше выводят ожидание из того же файла, поэтому файл,
        # потерявший все доменные таблицы, прошёл бы их незаметно. Пустой
        # белый список — это capability, которая отвечает «таблица не найдена»
        # на любой вопрос, и выглядит при этом рабочей.
        assert config["audit"]["tables"], (
            "в объявлении нет ни одной доменной таблицы: capability audit "
            "станет отвечать отказом на всё, не выдавая этого за поломку"
        )
        assert config["audit"]["row_ceiling"] > 0

    def test_registry_entry_is_excluded_from_domain_tables(self) -> None:
        config = _audit_config(_settings())
        registry = config["scripts_registry"]["table"]
        assert SCRIPTS_REGISTRY_LABEL, "метка реестра объявлена пустой строкой"
        assert all(
            registry not in table and registry.rsplit(".", 1)[-1] not in table
            for table in config["audit"]["tables"]
        ), (
            "реестр предустановленных скриптов попал в доменные таблицы: аудит "
            "смог бы читать собственные скрипты в обход проверки строк"
        )


class TestTheFileIsNotADecoration:
    def test_environment_wins_but_is_visible(self) -> None:
        """Приоритет окружения сохранён, но дубль больше не тихий."""
        settings = _settings(ENTERPRISE_AUDIT_TABLES="oarb.only_this")
        assert [name for name, _ in settings.get("ENTERPRISE_AUDIT_TABLES")] == [
            "oarb.only_this"
        ]
        assert settings.source("ENTERPRISE_AUDIT_TABLES") == (
            "env:ENTERPRISE_AUDIT_TABLES"
        ), "приоритет есть, а источник не виден — значит, непонятно, что применится"

    def test_environment_value_has_no_registry(self) -> None:
        """Строка не умеет сказать «это реестр» — и не должна врать."""
        config = _audit_config(_settings(ENTERPRISE_AUDIT_TABLES="oarb.only_this"))
        assert config["scripts_registry"]["table"] == ""

    def test_missing_key_refuses_to_start(self, tmp_path: Path) -> None:
        broken = _config_with(tmp_path, lambda raw: raw["audit"].pop("tables"))
        with pytest.raises(InfrastructureError) as exc:
            Settings(env=dict(DUMMY_SECRETS), file_path=broken, secrets={})
        assert TABLES_KEY in str(exc.value), (
            "ошибка обязана называть ключ: без него оператор ищет не туда"
        )

    def test_empty_list_refuses_to_start(self, tmp_path: Path) -> None:
        """Пустой список — это «аудит разрешает всё», а не «не задано»."""
        broken = _config_with(tmp_path, lambda raw: raw["audit"].__setitem__("tables", []))
        with pytest.raises(InfrastructureError) as exc:
            Settings(env=dict(DUMMY_SECRETS), file_path=broken, secrets={})
        assert TABLES_KEY in str(exc.value)

    def test_entry_without_name_refuses_to_start(self, tmp_path: Path) -> None:
        """Запись без имени — опечатка, а не «таблица с пустым именем»."""
        broken = _config_with(
            tmp_path, lambda raw: raw["audit"]["tables"].append({"label": "typo"})
        )
        with pytest.raises(InfrastructureError) as exc:
            Settings(env=dict(DUMMY_SECRETS), file_path=broken, secrets={})
        assert "ENTERPRISE_AUDIT_TABLES" in str(exc.value)
