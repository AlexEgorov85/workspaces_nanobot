-- ============================================================================
-- V003 — drop signature-store (change remove-vector-index-store)
-- ============================================================================
-- После change ``remove-vector-index-store`` persisted FAISS-кеш больше не
-- используется. FAISS-индекс собирается из DuckDB-снапшота ``storage_table``
-- на лету (``provider.preload_indexes`` при старте gateway или лениво в
-- ``search_vector``). Эта миграция удаляет таблицу.
--
-- Имя таблицы — из ``gateway.vector.index.signature_table`` (по умолчанию
-- в существующих развёрнутых инстансах — ``public.agent_vector_index_store``).
-- Оператор подставляет реальное имя при деплое. Если настройка не задана
-- или таблица уже отсутствует — миграция идемпотентна.
--
-- Совместимость: Greenplum 6.5 / PostgreSQL 12+.
-- ============================================================================

DROP TABLE IF EXISTS "<signature_table>";

-- Комментарий к записи в schema_migrations (если скрипт выполняется вне
-- tools/migrate.py — например, руками):
COMMENT ON SCHEMA public IS 'V003 dropped signature_store table';
