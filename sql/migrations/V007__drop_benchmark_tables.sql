-- ============================================================================
-- V007 — drop benchmark tables (change enterprise-mcp-platform, phase 1.5)
-- ============================================================================
-- Подсистема бенчмарков качества удалена целиком: пакет ``benchmarks/``
-- (runner, evaluator, scorer, loader, reporter, db, hooks, models) и его
-- настройки ``benchmark.{runs_table, results_table}``. Результаты прогонов
-- бенчмарков в PostgreSQL никогда не были источником истины для агента:
-- отчёты писались файлами в ``benchmarks/results/runs/`` через
-- ``reporter.save_json_report`` / ``save_markdown_report``, а таблицы
-- использовались только для накопления сырых прогонов.
--
-- Удаляются обе таблицы: ``results`` ссылается на ``runs`` через
-- ``run_id``, поэтому порядок важен.
--
-- НЕ удаляется: ``tests/benchmarks/``. Это каталог тестов quality-бенчмарков
-- навыка ``legal_summarizer`` (golden-dataset проверки), а не тесты пакета
-- ``benchmarks/``. Имя каталога вводит в заблуждение, но содержимое другое.
--
-- Номер 007 продолжает последовательность после V006
-- (V005 был занят удалённым V005__create_agent_cache_ownership.sql).
-- Миграция идемпотентна: IF EXISTS.
-- Совместимость: Greenplum 6.5 / PostgreSQL 12+.
-- ============================================================================

DROP TABLE IF EXISTS public.agent_benchmark_results;
DROP TABLE IF EXISTS public.agent_benchmark_runs;

-- Комментарий к записи в schema_migrations (если скрипт выполняется вне
-- tools/migrate.py — например, руками):
COMMENT ON SCHEMA public IS 'V007 dropped agent_benchmark_runs and agent_benchmark_results tables';

-- Регистрация версии выполняется runner'ом tools/migrate.py
-- (INSERT в public.schema_migrations) — НЕ этим файлом.
