"""Тесты для ``lib/services/preload_service.py``.

После рефакторинга ``refactor/core-extract-duckdb-faiss`` сервис
содержит только ``preload_vector_indexes`` (legacy CLI-методы
``preload_audit_cache`` / ``background_audit_cache_refresh`` удалены
как неиспользуемые — единственный писатель ``audit_cache.duckdb``
теперь ``DuckDbCacheStore.publish()`` через gateway).

Дополнительно — health-summary часть (vector-index divergence declared
vs runtime) эмитится в stderr и в ``agent_gateway_logs``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lib.services.preload_service import (
    PreloadService,
    compute_index_health,
    _format_lines,
    _emit_health_event,
)


# ============================================================================
# Pure-функция ``compute_index_health``
# ============================================================================


def _declared(name: str = "audits_index",
              source_table: str = "oarb.audits",
              chunk_size: int = 500,
              dimension: int = 1024,
              **extra) -> dict:
    base = {
        name: {
            "table": source_table.replace(".", "_"),
            "pk": "id",
            "source_table": source_table,
            "content_columns": ["title"],
            "embedding_columns": ["title_emb"],
            "track_column": "updated_at",
            "chunk_size": chunk_size,
            "chunk_overlap": 80,
            "metric": "cosine",
            "embedding_model": "mxbai-embed-large",
            "dimension": dimension,
            "enabled": True,
            **extra,
        },
    }
    return base


def _runtime_row(name: str = "audits_index",
                vector_count: int = 100,
                dimension: int = 1024,
                metric: str = "cosine",
                signature: str | None = None,
                metadata: dict | None = None) -> dict:
    meta = {"metric": metric}
    if signature is not None:
        meta["signature"] = signature
    if metadata:
        meta.update(metadata)
    return {
        "source": name,
        "dimension": dimension,
        "vector_count": vector_count,
        "updated_at": "2026-09-14T10:00:00+00:00",
        "metadata": meta,
    }


class TestComputeIndexHealth:
    def test_all_ok_full_match(self) -> None:
        """declared == loaded == runtime всё CURRENT → нет divergence."""
        from lib.services.cache_provider_impl import compute_index_signature

        cfg = _declared()
        sig = compute_index_signature(cfg["audits_index"])
        loaded = [{"index_name": "audits_index", "vectors": 100}]
        runtime = [_runtime_row(signature=sig)]
        h = compute_index_health(cfg, loaded, runtime)
        assert h["missing"] == []
        assert h["orphan"] == []
        assert h["stale"] == []
        assert h["divergence"] is False
        assert h["level"] == "INFO"

    def test_missing_when_declared_but_not_loaded(self) -> None:
        """Declared есть, loaded пуст → missing + divergence."""
        cfg = _declared()
        h = compute_index_health(cfg, [], None)
        assert h["missing"] == ["audits_index"]
        assert h["divergence"] is True
        assert h["level"] == "WARN"

    def test_missing_when_loaded_other(self) -> None:
        """Declared [a, b], loaded только [a] → missing = [b]."""
        cfg = {
            "a": _declared()["audits_index"],
            "b": _declared()["audits_index"],
        }
        cfg["a"]["source_table"] = "oarb.first"
        cfg["b"]["source_table"] = "oarb.second"
        cfg = {
            "audits_index": cfg["a"],
            "reports_index": cfg["b"],
        }
        loaded = [{"index_name": "audits_index", "vectors": 100}]
        h = compute_index_health(cfg, loaded, None)
        assert h["missing"] == ["reports_index"]
        assert h["loaded_items"][0]["index_name"] == "audits_index"

    def test_orphan_when_runtime_only(self) -> None:
        """PG store содержит blob, не объявленный в JSON → orphan."""
        cfg = _declared()
        runtime = [
            _runtime_row(name="audits_index"),
            _runtime_row(name="legacy_orphan", vector_count=10),
        ]
        h = compute_index_health(cfg, [], runtime)
        assert "legacy_orphan" in h["orphan"]
        assert "audits_index" in h["missing"]  # declared but not loaded (например, store не собран)
        assert h["divergence"] is True

    def test_handles_runtime_none_gracefully(self) -> None:
        """``runtime_rows=None`` (PG недоступна) → orphan/stale пусты, но не raise."""
        cfg = _declared()
        h = compute_index_health(cfg, [], None)
        assert h["orphan"] == []
        # declared not loaded → missing всё равно видим.
        assert h["missing"] == ["audits_index"]

    def test_loaded_none_treated_as_empty(self) -> None:
        """``loaded=None`` (preload упал) → loaded пуст, divergence по missing."""
        cfg = _declared()
        h = compute_index_health(cfg, None, None)
        assert h["loaded_items"] == []
        assert h["missing"] == ["audits_index"]
        assert h["divergence"] is True


# ============================================================================
# Format
# ============================================================================


class TestFormatLines:
    def test_format_shows_declared_count(self) -> None:
        lines = _format_lines(
            declared_names=["audits_index", "violations_index"],
            loaded_items=[{"index_name": "audits_index", "vectors": 429}],
            missing=["violations_index"],
            orphan=[],
            stale=["audits_index:STALE"],
        )
        text = "\n".join(lines)
        assert "declared (2)" in text
        assert "loaded   (1)" in text
        assert "missing  (1): violations_index" in text
        assert "stale    (1): audits_index:STALE" in text
        assert "orphan   (0)" in text

    def test_format_dash_when_empty(self) -> None:
        lines = _format_lines(
            declared_names=[],
            loaded_items=[],
            missing=[],
            orphan=[],
            stale=[],
        )
        text = "\n".join(lines)
        assert "declared (0): —" in text
        assert "loaded   (0): —" in text
        assert "missing  (0)" in text


# ============================================================================
# _emit_health_event (dual-sink)
# ============================================================================


class TestEmitHealthEvent:
    def test_emits_with_full_payload(self) -> None:
        """Дёргает ``try_log_event`` из ``lib.services.db_logging_service``
        с правильными аргументами (event_type, level, summary, payload, service).
        """
        with patch("lib.services.db_logging_service.try_log_event") as ev:
            _emit_health_event(
                summary="declared=3 loaded=2",
                payload={"declared": ["a"], "missing": ["b"]},
                level="WARN",
                service="<db_logging_service>",
            )
        ev.assert_called_once()
        args, kwargs = ev.call_args
        assert kwargs["producer"] == "PreloadService"
        assert kwargs["event_type"] == "vector_index_preload_health"
        assert args[0] == "<db_logging_service>"
        log_event = args[1]
        assert log_event.event_type == "vector_index_preload_health"
        assert log_event.level == "WARN"
        assert log_event.summary == "declared=3 loaded=2"
        assert log_event.payload == {"declared": ["a"], "missing": ["b"]}

    def test_emits_swallows_exceptions(self) -> None:
        """``_emit_health_event`` полагается на ``try_log_event``, который
        сам глотает исключения. Тест проверяет, что helper вызывает
        ``try_log_event`` (без своей обёртки try/except).
        """
        with patch(
            "lib.services.db_logging_service.try_log_event",
            return_value=False,
        ) as ev:
            _emit_health_event(
                summary="x",
                payload={},
                level="INFO",
                service=None,
            )
        ev.assert_called_once()


# ============================================================================
# Integration: ``preload_vector_indexes`` эмитит health summary
# ============================================================================


class TestPreloadEmitsHealth:
    @pytest.mark.asyncio
    async def test_preload_writes_to_stderr(self, capsys) -> None:
        """stderr захватывает emitted summary при успешном preload."""
        from lib.services.cache_provider_impl import compute_index_signature

        cfg = _declared()
        sig = compute_index_signature(cfg["audits_index"])
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.return_value = [
            {"index_name": "audits_index", "vectors": 100}
        ]
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=cfg,
        ), patch(
            "lib.services.cache_provider_impl.list_runtime_vector_indexes",
            return_value=[_runtime_row(signature=sig)],
        ):
            svc = PreloadService(db_logging_service=None)
            await svc.preload_vector_indexes(store)

        captured = capsys.readouterr()
        assert "[vector] preload health summary:" in captured.err
        assert "declared (1)" in captured.err
        assert "loaded   (1): audits_index(100)" in captured.err

    @pytest.mark.asyncio
    async def test_preload_emits_db_event_when_service_given(self) -> None:
        """Если передан db_logging_service — ``emit_sync_event`` зовётся с WARN."""
        from lib.services.cache_provider_impl import compute_index_signature

        cfg = _declared()
        store = MagicMock()
        store.is_ready.return_value = True
        # После change ``remove-vector-index-store`` STALE берётся
        # из loaded_items[i]["signature_status"] (inline-вычисленный
        # провайдером при preload). Тест симулирует это явно.
        store.preload_indexes.return_value = [
            {"index_name": "audits_index", "vectors": 100, "signature_status": "STALE"},
        ]
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=cfg,
        ), patch(
            "lib.services.cache_provider_impl.list_runtime_vector_indexes",
            return_value=[_runtime_row()],
        ), patch(
            "lib.services.db_logging_service.try_log_event"
        ) as ev:
            svc = PreloadService(db_logging_service="<svc>")
            await svc.preload_vector_indexes(store)

        # Должно быть STALE → divergence=True → level=WARN
        ev.assert_called_once()
        args, kwargs = ev.call_args
        assert kwargs["producer"] == "PreloadService"
        assert kwargs["event_type"] == "vector_index_preload_health"
        assert args[0] == "<svc>"
        log_event = args[1]
        assert log_event.level == "WARN"
        assert "audits_index:STALE" in log_event.payload["stale"]

    @pytest.mark.asyncio
    async def test_preload_summary_when_loaded_is_none(self, capsys) -> None:
        """Если preload_indexes упал (loaded=None) → summary всё равно пишется."""
        cfg = _declared()
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.side_effect = RuntimeError("boom")
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=cfg,
        ), patch(
            "lib.services.cache_provider_impl.list_runtime_vector_indexes",
            return_value=[],
        ):
            svc = PreloadService()
            await svc.preload_vector_indexes(store)  # не бросает

        captured = capsys.readouterr()
        assert "[vector] preload health summary:" in captured.err
        # missing = declared (loaded пуст).
        assert "missing  (1)" in captured.err

    @pytest.mark.asyncio
    async def test_preload_survives_runtime_failure(self) -> None:
        """PG store raise → summary не валится, считает только по declared."""
        cfg = _declared()
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.return_value = []
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=cfg,
        ), patch(
            "lib.services.cache_provider_impl.list_runtime_vector_indexes",
            side_effect=RuntimeError("PG down"),
        ):
            svc = PreloadService()
            # Не должно быть исключения
            result = await svc.preload_vector_indexes(store)
        assert result == []  # loaded — fallback на "loaded НЕ None", пустой

    @pytest.mark.asyncio
    async def test_preload_emits_db_event_INFO_when_no_divergence(self) -> None:
        """declared == loaded, signature CURRENT → level=INFO (не WARN)."""
        from lib.services.cache_provider_impl import compute_index_signature

        cfg = _declared()
        sig = compute_index_signature(cfg["audits_index"])
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.return_value = [
            {"index_name": "audits_index", "vectors": 100}
        ]
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=cfg,
        ), patch(
            "lib.services.cache_provider_impl.list_runtime_vector_indexes",
            return_value=[_runtime_row(signature=sig)],
        ), patch(
            "lib.services.db_logging_service.try_log_event"
        ) as ev:
            svc = PreloadService(db_logging_service="<svc>")
            await svc.preload_vector_indexes(store)

        args = ev.call_args.args
        log_event = args[1]
        assert log_event.level == "INFO"
        assert log_event.payload["missing"] == []
        assert log_event.payload["stale"] == []
        assert log_event.payload["orphan"] == []


# ============================================================================
# Back-compat: original return shape сохранён
# ============================================================================


class TestPreloadReturnShape:
    @pytest.mark.asyncio
    async def test_store_not_ready_returns_none(self):
        store = MagicMock()
        store.is_ready.return_value = False
        svc = PreloadService()
        assert await svc.preload_vector_indexes(store) is None

    @pytest.mark.asyncio
    async def test_ready_preloads(self):
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.return_value = [{"index_name": "a", "vectors": 10}]
        svc = PreloadService()
        result = await svc.preload_vector_indexes(store)
        assert result == [{"index_name": "a", "vectors": 10}]

    @pytest.mark.asyncio
    async def test_error_returns_none(self):
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.side_effect = RuntimeError("boom")
        svc = PreloadService()
        assert await svc.preload_vector_indexes(store) is None

    @pytest.mark.asyncio
    async def test_none_store_returns_none(self):
        svc = PreloadService()
        assert await svc.preload_vector_indexes(None) is None

    @pytest.mark.asyncio
    async def test_db_logging_optional(self):
        """``db_logging_service=None`` — summary всё равно пишется в stderr."""
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.return_value = []
        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=_declared(),
        ), patch(
            "lib.services.cache_provider_impl.list_runtime_vector_indexes",
            return_value=[],
        ):
            svc = PreloadService(db_logging_service=None)
            await svc.preload_vector_indexes(store)
        # Не должно быть AttributeError из-за отсутствия db_logging_service.
        # Если isinstance(None, ...) не сработает, emit_health_event просто
        # вызовет emit_sync_event с service=None — что нормально.