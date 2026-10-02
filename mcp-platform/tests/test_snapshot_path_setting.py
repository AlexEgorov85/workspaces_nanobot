"""Разбор объявленного пути снимка: ``~`` → домашний каталог.

Проверяется не «путь развернулся», а три свойства, из-за которых разбор
живёт в пакете снимка, а не в реестре настроек и не в конфиге:

* пустой остаётся пустым — «снимок не настроен» должно оставаться выразимым
  состоянием, а не молча превращаться в путь по умолчанию;
* значение без ``~`` не трогается, включая ``{...}`` и пробелы;
* неразворачиваемый ``~`` падает с именем настройки, а не уходит в DuckDB.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from libs.enterprise_data.snapshot.contracts import CacheOpenError
from libs.enterprise_data.snapshot.store import resolve_snapshot_setting

SETTING = "ENTERPRISE_SNAPSHOT_PATH"


class TestHomeExpansion:
    def test_tilde_expands_to_home(self) -> None:
        resolved = resolve_snapshot_setting("~/.cache/nanobot/duckdb/cache.duckdb", SETTING)

        assert "~" not in resolved
        assert resolved == str(
            Path("~/.cache/nanobot/duckdb/cache.duckdb").expanduser()
        )

    def test_bare_tilde_is_home(self) -> None:
        assert resolve_snapshot_setting("~", SETTING) == str(Path.home())

    def test_expanded_path_is_absolute(self) -> None:
        assert Path(resolve_snapshot_setting("~/x.duckdb", SETTING)).is_absolute()


class TestUntouchedValues:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            None,
            "cache.duckdb",
            "C:/data/cache.duckdb",
            "/var/lib/nanobot/cache.duckdb",
            "~/already/absolute.duckdb".replace("~", ""),
        ],
    )
    def test_value_without_leading_tilde_is_returned_as_is(self, value) -> None:
        """Относительный путь тоже остаётся как есть.

        Он неверный по смыслу (разрешается от текущего каталога процесса), но
        это не задача разбора: молча «улучшать» его значило бы менять
        поведение, о котором объявление не говорит.
        """
        result = resolve_snapshot_setting(value, SETTING)

        assert result == str(value or "").strip()

    def test_surrounding_spaces_are_trimmed(self) -> None:
        assert resolve_snapshot_setting("  /data/cache.duckdb  ", SETTING) == (
            "/data/cache.duckdb"
        )


class TestLoudFailure:
    def test_unresolvable_home_raises_naming_the_setting(self, monkeypatch) -> None:
        """Молчаливый откат здесь недопустим.

        Если развёрнуть ``~`` не удалось и оставить строку как есть, ошибку
        сообщит уже DuckDB — и в ней не будет сказано, что дело в настройке.

        Проверяется контракт обработки, а не поведение платформы: на POSIX
        ``expanduser`` падает сам для несуществующего пользователя, а на
        Windows подставляет имя молча. Платформенно-зависимое «падает или нет»
        проверять нельзя — на одной машине тест был бы зелёным, на другой
        красным по причине, не имеющей отношения к коду.
        """
        from pathlib import Path

        def _boom(self):  # noqa: ANN001 - подмена метода
            raise RuntimeError("home is not resolvable")

        monkeypatch.setattr(Path, "expanduser", _boom)

        with pytest.raises(CacheOpenError) as exc:
            resolve_snapshot_setting("~/cache.duckdb", SETTING)

        assert SETTING in str(exc.value)
