"""``compute_index_health``: откуда берутся stale / orphan / missing.

**Страж портирован из агента** (``tests/test_remove_vector_index_store_guards.py``,
класс ``TestComputeIndexHealthNewSources``) в момент удаления кластера снимка
из агента, 2026-10-01. Пока агентский вариант существовал, он был единственным
покрытием этой функции; вместе с удалением кода исчез бы и он, а на платформе
инварианты не охранял никто — типичная дыра, которая не выглядит дырой, пока
код на месте.

Логика функции на платформе и в агенте одна и та же, поэтому тесты перенесены
дословно по смыслу, а не переписаны под другой API.

**Производственного вызова нет — и это оставлено осознанно, тест удалять нельзя.**
Канон снял требование «Preload health-summary виден оператору и логируется»
(change ``2026-10-05-vector-indexes-canon-gap``, ``## REMOVED Requirements``):
публиковать сводку было некому, ``PreloadService`` удалён. Снималась публикация,
а не расчёт: ``compute_index_health`` осталась и остаётся посчитанной. Возврат
публикации — отдельный change с адресатом и порогом; до него единственный
вызов функции — этот файл. Если тест убрать как «мёртвый», инварианты
stale / orphan / missing останутся без охраны, и никто об этом не узнает.
"""

from __future__ import annotations

from libs.vectors.preload import compute_index_health

DECLARED = {"audits_index": {"table": "t", "pk": "id"}}


class TestStaleFromLoadedSignature:
    """``stale`` берётся из ``signature_status`` загруженного индекса.

    Источник принципиален: подпись сравнивается с тем, что реально лежит в
    хранилище. Если взять статус из объявления, индекс, чья подпись разошлась
    с данными, будет выглядеть исправным ровно до следующей перезагрузки.
    """

    def test_stale_is_reported_and_downgrades_level(self) -> None:
        loaded = [
            {"index_name": "audits_index", "vectors": 100, "signature_status": "STALE"},
        ]
        runtime_rows = [{"source": "audits_index", "vector_count": 100}]

        health = compute_index_health(DECLARED, loaded, runtime_rows)

        assert "audits_index:STALE" in health["stale"]
        assert health["divergence"] is True
        assert health["level"] == "WARN"

    def test_invalid_signature_is_stale_too(self) -> None:
        """``INVALID`` — тоже расхождение, не «всё хорошо»."""
        loaded = [
            {"index_name": "audits_index", "vectors": 100, "signature_status": "INVALID"},
        ]
        runtime_rows = [{"source": "audits_index", "vector_count": 100}]

        health = compute_index_health(DECLARED, loaded, runtime_rows)

        assert "audits_index:INVALID" in health["stale"]
        assert health["level"] == "WARN"

    def test_current_signature_is_not_stale(self) -> None:
        loaded = [
            {"index_name": "audits_index", "vectors": 100, "signature_status": "CURRENT"},
        ]
        runtime_rows = [{"source": "audits_index", "vector_count": 100}]

        health = compute_index_health(DECLARED, loaded, runtime_rows)

        assert health["stale"] == []
        assert health["divergence"] is False
        assert health["level"] == "INFO"


class TestOrphanFromStorage:
    """``orphan`` берётся из хранилища, ``missing`` — из объявления.

    Вместе они закрывают обе стороны рассогласования: индекс, оставшийся в
    базе после удаления из объявления (orphan), и объявленный, но не
    собранный (missing). Односторонняя проверка оставляет ровно один из двух
    случаев невидимым — причём тот, что выглядит здоровее.
    """

    def test_orphan_and_missing_are_split(self) -> None:
        loaded: list[dict] = []
        runtime_rows = [
            {"source": "audits_index", "vector_count": 100},
            {"source": "legacy_index", "vector_count": 50},
        ]

        health = compute_index_health(DECLARED, loaded, runtime_rows)

        assert "legacy_index" in health["orphan"]
        assert "audits_index" in health["missing"]
        assert health["divergence"] is True

    def test_snapshot_unavailable_reports_nothing_about_orphans(self) -> None:
        """Нет снимка — не значит «orphan не найден».

        Без ``runtime_rows`` функция обязана вернуть пустые ``orphan`` и
        ``stale`` и не объявлять расхождение: иначе недоступность хранилища
        читалась бы как «состав индексов испорчен», и оператор пошёл бы чинить
        не то.
        """
        loaded = [
            {"index_name": "audits_index", "vectors": 100, "signature_status": "CURRENT"},
        ]

        health = compute_index_health(DECLARED, loaded, None)

        assert health["orphan"] == []
        assert health["stale"] == []
        assert health["divergence"] is False
        assert health["level"] == "INFO"
