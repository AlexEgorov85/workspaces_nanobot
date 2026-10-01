"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``.

все 14 тестов портированы в mcp-platform/tests/test_vectors_indexing.py (TestAsVector, TestBuildFaissIndex, TestBuildRawItems)

Кластер снимка удалён из агента: снимком, индексами и эмбеддингами владеют
capability ``data`` / ``vectors`` / ``llm`` платформы.
Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Содержимое вырезано (149 строк), имя начинается с
подчёркивания, поэтому pytest его не собирает.
Удалить вручную: git rm tests/test_vector_search_silent_failure.py
"""
