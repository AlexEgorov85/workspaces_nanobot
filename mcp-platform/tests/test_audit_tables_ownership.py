"""Перенос списка таблиц аудита в ``platform.json`` — чем он защищён.

Дефект, который ловит этот файл
--------------------------------

Таблицы аудита и реестр предустановленных скриптов объявлялись агентом
(``project.json → skills.audit_analyzer.tables``) и передавались процессу
переменными ``ENTERPRISE_AUDIT_TABLES`` / ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE``.
Формально MCP читал настройки агента, а по факту знание о проекте лежало в
чужом окружении: сломанный экспорт выключал capability ``audit`` целиком и
молча (``registry_unavailable`` на каждую операцию).

Теперь это платформенные значения в файле. Проверяем ровно то свойство,
ради которого перенос состоялся: **файл — источник, а не декорация.**

* значения приходят из файла, а не из окружения процесса;
* окружение по-прежнему перебивает файл (развёртывание может задать руками),
  но тогда источник виден как ``env:*`` — молчаливого дубля не остаётся;
* отсутствующий или пустой ключ останавливает сервер с именем ключа, а не
  превращается в пустой белый список;
* реестр скриптов не попадает в белый список: его label в ``project.json``
  означает «метаданные, не схема для модели», и выдав его как доменную
  таблицу, мы разрешили бы аудиту читать собственные скрипты в обход
  проверки строк.
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
    Settings,
)
from servers.enterprise.server import _audit_config

from conftest import DUMMY_SECRETS

TABLES_KEY = "audit.tables"
REGISTRY_KEY = "audit.scripts_registry_table"


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


class TestAuditTablesAreDeclaredInTheFile:
    def test_registry_says_the_file_is_the_owner(self) -> None:
        for name in ("ENTERPRISE_AUDIT_TABLES", "ENTERPRISE_SCRIPTS_REGISTRY_TABLE"):
            setting = BY_NAME[name]
            assert setting.owner == OWNER_PLATFORM, (
                f"{name}: владелец {setting.owner!r} — значит, значение снова "
                f"придёт из окружения агента"
            )
            assert setting.default is FROM_FILE, (
                f"{name}: в коде есть значение {setting.default!r}; пока оно "
                f"не FROM_FILE, ключ в файле можно забыть, и сервер поднимется"
            )
            assert setting.key in (TABLES_KEY, REGISTRY_KEY)

    def test_tables_come_from_the_file(self) -> None:
        settings = _settings()
        declared = settings.get("ENTERPRISE_AUDIT_TABLES")
        file_value = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        assert settings.source("ENTERPRISE_AUDIT_TABLES") == "file:platform.json"
        assert declared == file_value["audit"]["tables"], (
            "значение из файла не совпало с тем, что в самом файле"
        )
        assert declared, "пустой белый список: аудит отвечал бы всему подряд"

    def test_registry_table_is_not_in_the_white_list(self) -> None:
        settings = _settings()
        tables = settings.get("ENTERPRISE_AUDIT_TABLES")
        registry = settings.get("ENTERPRISE_SCRIPTS_REGISTRY_TABLE")
        assert registry, "таблица реестра скриптов не объявлена"
        assert all(registry not in t and registry.rsplit(".", 1)[-1] not in t
                   for t in tables), (
            "реестр предустановленных скриптов попал в белый список доменных "
            "таблиц: аудит смог бы читать собственные скрипты в обход "
            "проверки строк"
        )

    def test_capability_config_gets_both(self) -> None:
        config = _audit_config(_settings())
        assert config["scripts_registry"]["table"]
        assert config["audit"]["tables"]
        assert config["audit"]["row_ceiling"] > 0


class TestTheFileIsNotADecoration:
    def test_environment_wins_but_is_visible(self) -> None:
        """Приоритет окружения сохранён, но дубль больше не тихий."""
        settings = _settings(ENTERPRISE_AUDIT_TABLES="oarb.only_this")
        assert settings.get("ENTERPRISE_AUDIT_TABLES") == ["oarb.only_this"]
        assert settings.source("ENTERPRISE_AUDIT_TABLES") == (
            "env:ENTERPRISE_AUDIT_TABLES"
        ), "приоритет есть, а источник не виден — значит, непонятно, что применится"

    def test_missing_key_refuses_to_start(self, tmp_path: Path) -> None:
        broken = _config_with(tmp_path, lambda raw: raw["audit"].pop("tables"))
        with pytest.raises(InfrastructureError) as exc:
            Settings(env=dict(DUMMY_SECRETS), file_path=broken, secrets={})
        assert TABLES_KEY in str(exc.value), (
            "ошибка обязана называть ключ: без него оператор ищет не туда"
        )

    def test_empty_key_refuses_to_start(self, tmp_path: Path) -> None:
        """Пустой список — это «аудит разрешает всё», а не «не задано»."""
        broken = _config_with(tmp_path, lambda raw: raw["audit"].__setitem__("tables", []))
        with pytest.raises(InfrastructureError) as exc:
            Settings(env=dict(DUMMY_SECRETS), file_path=broken, secrets={})
        assert TABLES_KEY in str(exc.value)

    def test_missing_registry_table_refuses_to_start(self, tmp_path: Path) -> None:
        broken = _config_with(
            tmp_path, lambda raw: raw["audit"].pop("scripts_registry_table")
        )
        with pytest.raises(InfrastructureError) as exc:
            Settings(env=dict(DUMMY_SECRETS), file_path=broken, secrets={})
        assert REGISTRY_KEY in str(exc.value)
