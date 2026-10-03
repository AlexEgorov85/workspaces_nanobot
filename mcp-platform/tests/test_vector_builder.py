"""Инкрементальная сборка векторов: сценарии, а не «сборка вообще».

Проверяется поведение, перенесённое из агентского ``tools/build_vectors.py``.
Главное, что здесь защищается, — не «вставилось N векторов», а **что не
вставилось лишнего**: переэмбеддинг неизменившейся строки означает деньги и
время, а удаление вектора раньше успешной вставки нового означает потерю строки
из выдачи.

База и эмбеддер — подставные объекты. Тест не проверяет SQL против живой БД:
он проверяет решения сборщика, а корректность текста запроса проверяет
отдельный страж (см. :class:`_RecordingDb`).
"""

from __future__ import annotations

import pytest
from libs.vectors import builder as b


class _RecordingDb:
    """Пул PostgreSQL, который отвечает из заготовок и помнит записи."""

    def __init__(
        self,
        *,
        existing: list[dict] | None = None,
        source_rows: list[dict] | None = None,
        max_track: str | None = None,
    ) -> None:
        self._existing = existing or []
        self._source = source_rows or []
        self._max_track = max_track
        self.executed: list[tuple[str, tuple]] = []
        #: Классы работы, которыми сборщик пометил каждый вызов. Проверяется
        #: тестом: сборка идёт при подъёме платформы, то есть это работа
        #: системы, и ушедшая в класс модели отказ уронил бы чужую сборку.
        self.audiences: list[str] = []

    def fetch(self, sql: str, *args, audience: str):
        self.audiences.append(audience)
        if "content_hash" in sql and "SELECT pk_value" in sql:
            return list(self._existing)
        if sql.lstrip().upper().startswith("SELECT * FROM"):
            return [dict(r) for r in self._source]
        raise AssertionError(f"непредвиденный SELECT: {sql}")

    def fetchone(self, sql: str, *args, audience: str):
        self.audiences.append(audience)
        if "MAX(" in sql:
            return {"mx": self._max_track}
        raise AssertionError(f"непредвиденный fetchone: {sql}")

    def execute(self, sql: str, *args, audience: str):
        self.audiences.append(audience)
        self.executed.append((" ".join(sql.split()), args))
        return None

    def inserts(self) -> list[tuple[str, tuple]]:
        return [e for e in self.executed if e[0].lstrip().upper().startswith("INSERT")]

    def deletes(self) -> list[tuple[str, tuple]]:
        return [e for e in self.executed if e[0].lstrip().upper().startswith("DELETE")]


class _CountingEmbed:
    """Эмбеддер, считающий вызовы: «сеть» здесь видна как их число."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_for = fail_for or set()

    def __call__(self, text: str) -> list[float] | None:
        self.calls.append(text)
        if text in self.fail_for:
            return None
        return [0.1, 0.2, 0.3]


INDEX = {
    "check_entity": {
        "table": "oarb.audits",
        "pk": "id",
        "source_table": "oarb.audits",
        "content_columns": ["text"],
        "embedding_columns": ["text"],
        "track_column": "updated_at",
        "enabled": True,
    }
}


def _builder(db, embed, **kw) -> b.VectorBuilder:
    params = {
        "db": db,
        "embed": embed,
        "storage_table": "oarb.audit_vectors",
        "indexes": INDEX,
        "chunk_defaults": {"chunk_size": 500, "chunk_overlap": 80},
        "embedding_retry_wait": 0.0,
        "sleeper": lambda _s: None,
    }
    params.update(kw)
    return b.VectorBuilder(**params)


def _row(pk: int, text: str) -> dict:
    return {"id": pk, "text": text, "updated_at": "2026-01-01"}


def _stored(pk: int, text: str) -> dict:
    return {
        "pk_value": str(pk),
        "content_hash": b.content_hash(f"text: {text}"),
        "chunk_count": 1,
    }


class TestIncremental:
    def test_new_row_is_embedded_and_inserted(self) -> None:
        db = _RecordingDb(source_rows=[_row(1, "привет")])
        embed = _CountingEmbed()

        result = _builder(db, embed).build_index("check_entity")

        assert result.inserted == 1
        assert result.errors == 0
        assert len(embed.calls) == 1
        # Класс работы объявлен на каждом вызове и он именно системный: сборка
        # идёт при подъёме платформы, и ушедшая в модельный класс работа
        # заняла бы место, объявленное резервом.
        assert db.audiences, "сборщик обязан объявлять класс каждого вызова"
        assert set(db.audiences) == {"runtime"}, db.audiences
        assert len(db.inserts()) == 1
        sql, args = db.inserts()[0]
        assert '"table"' in sql
        assert args[0] == "check_entity"
        assert args[3] == "oarb.audits"
        assert args[4] == "1"

    def test_unchanged_row_costs_no_embedding_and_no_write(self) -> None:
        """Главная экономия: пустой diff не должен ходить в провайдера."""
        db = _RecordingDb(
            source_rows=[_row(1, "привет")], existing=[_stored(1, "привет")]
        )
        embed = _CountingEmbed()

        result = _builder(db, embed).build_index("check_entity")

        assert result.skipped is True
        assert result.reason == "изменений нет"
        assert result.unchanged == 1
        assert embed.calls == []
        assert db.executed == []

    def test_changed_row_replaces_hash_after_insert(self) -> None:
        db = _RecordingDb(
            source_rows=[_row(1, "новый")], existing=[_stored(1, "старый")]
        )
        embed = _CountingEmbed()

        result = _builder(db, embed).build_index("check_entity")

        assert result.inserted == 1
        assert result.updated == 1
        # Удаление старого идёт по «хеш <> новый», и только после вставки.
        assert len(db.deletes()) == 1
        delete_sql, delete_args = db.deletes()[0]
        assert "content_hash <> %s" in delete_sql
        assert delete_args[2] == "1"
        assert delete_args[3] == b.content_hash("text: новый")
        assert db.executed.index(db.inserts()[0]) < db.executed.index(db.deletes()[0])

    def test_row_removed_from_source_is_deleted(self) -> None:
        db = _RecordingDb(source_rows=[], existing=[_stored(7, "была")])
        embed = _CountingEmbed()

        result = _builder(db, embed).build_index("check_entity")

        assert result.deleted == 1
        assert embed.calls == []
        _, args = db.deletes()[0]
        assert args[2] == "7"

    def test_lost_content_removes_stale_vector(self) -> None:
        """Строка осталась, но эмбеддить стало нечего — вектор обязан уйти."""
        db = _RecordingDb(
            source_rows=[{"id": 9, "text": "   ", "updated_at": "2026-01-01"}],
            existing=[_stored(9, "была")],
        )
        embed = _CountingEmbed()

        result = _builder(db, embed).build_index("check_entity")

        assert result.deleted == 1
        assert embed.calls == []

    def test_failed_embedding_keeps_previous_vector(self) -> None:
        """Новый эмбеддинг не удался — старый вектор сохраняется.

        Обратный порядок (удалить раньше вставить) оставил бы строку вообще
        без вектора, то есть выдача потеряла бы её молча.
        """
        db = _RecordingDb(
            source_rows=[_row(1, "новый")], existing=[_stored(1, "старый")]
        )
        embed = _CountingEmbed(fail_for={"text: новый"})

        result = _builder(db, embed).build_index("check_entity")

        assert result.errors == 1
        assert result.inserted == 0
        assert result.updated == 0
        assert db.deletes() == []


class TestModes:
    def test_full_rebuild_clears_source_before_insert(self) -> None:
        db = _RecordingDb(
            source_rows=[_row(1, "привет")], existing=[_stored(5, "старое")]
        )
        embed = _CountingEmbed()

        result = _builder(db, embed, full_rebuild=True).build_index("check_entity")

        assert result.inserted == 1
        # Полная очистка идёт до вставки, иначе новые векторы попадут под нож.
        assert db.executed[0][0].lstrip().upper().startswith("DELETE")
        assert len(db.inserts()) == 1

    def test_dry_run_touches_nothing(self) -> None:
        db = _RecordingDb(
            source_rows=[_row(1, "привет")], existing=[_stored(2, "исчезла")]
        )
        embed = _CountingEmbed()

        result = _builder(db, embed, dry_run=True).build_index("check_entity")

        assert result.total == 1
        assert db.executed == []
        # Даже эмбеддер не дёргается: dry-run показывает план, а не тратит его.
        assert embed.calls == []

    def test_progress_is_reported_per_chunk(self) -> None:
        db = _RecordingDb(source_rows=[_row(1, "а"), _row(2, "б")])
        embed = _CountingEmbed()
        seen: list[tuple[int, int]] = []

        _builder(
            db, embed, progress=lambda p: seen.append((p.done, p.total))
        ).build_index("check_entity")

        assert seen == [(1, 2), (2, 2)]


class TestDeclaration:
    def test_unknown_index_raises_instead_of_building_nothing(self) -> None:
        db, embed = _RecordingDb(), _CountingEmbed()

        with pytest.raises(ValueError, match="не объявлен"):
            _builder(db, embed).build_index("нет_такого")

    def test_incomplete_declaration_is_skipped_with_reason(self) -> None:
        db, embed = _RecordingDb(), _CountingEmbed()
        builder = b.VectorBuilder(
            db=db,
            embed=embed,
            storage_table="oarb.audit_vectors",
            indexes={"broken": {"table": "oarb.audits", "enabled": True}},
        )

        result = builder.build_index("broken")

        assert result.skipped is True
        assert "pk" in result.reason
        assert db.executed == []

    def test_disabled_index_is_not_selected(self) -> None:
        db, embed = _RecordingDb(), _CountingEmbed()
        builder = b.VectorBuilder(
            db=db,
            embed=embed,
            storage_table="oarb.audit_vectors",
            indexes={
                "off": {**INDEX["check_entity"], "enabled": False},
            },
        )

        assert builder.index_names() == []
        with pytest.raises(ValueError, match="выключен"):
            builder.index_names("off")


class TestQuoting:
    @pytest.mark.parametrize(
        "bad", ["", "oarb.audit vectors", "1oarb", "oarb; DROP TABLE x", "a.b.c.d x"]
    )
    def test_malformed_table_name_fails_loudly(self, bad: str) -> None:
        """Имя таблицы приходит из конфигурации, но подставляется в SQL.

        Молчаливый обрыв здесь выглядел бы как «индекс не собрался» — то есть
        как проблема данных при опечатке в настройке.
        """
        with pytest.raises(ValueError, match="идентификатор"):
            b.quote_table(bad)

    def test_qualified_name_is_quoted(self) -> None:
        assert b.quote_table("oarb.audit_vectors") == '"oarb"."audit_vectors"'
        assert b.quote_table("audit_vectors") == '"audit_vectors"'

    def test_column_quoting_names_the_column(self) -> None:
        with pytest.raises(ValueError, match="колонки"):
            b.quote_identifier("плохое имя")

    def test_float_pk_is_canonicalised(self) -> None:
        """1.0 и 1 — один ключ; иначе пересборка считала бы строку новой."""
        assert b.norm_pk(1.0) == "1"
        assert b.norm_pk(1) == "1"
        assert b.norm_pk(None) == ""


class TestHelpers:
    def test_embedding_columns_may_be_objects(self) -> None:
        assert b.normalize_embedding_cols(
            ["a", {"column": "b", "chunk": True}, {"no_column": 1}, ""]
        ) == ["a", "b"]

    def test_search_text_labels_columns_and_skips_empty(self) -> None:
        text = b.build_search_text({"a": "раз", "b": "  ", "c": "два"}, ["a", "b", "c"])
        assert text == "a: раз. c: два"

    def test_content_hash_changes_with_text(self) -> None:
        assert b.content_hash("x") != b.content_hash("y")
        assert b.content_hash("x") == b.content_hash("x")
