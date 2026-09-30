"""Enterprise-библиотеки. Не зависят от агента.

* ``enterprise_common`` — конфиг, модели, ошибки, сериализация.
* ``enterprise_data``   — доступ к данным: PostgreSQL, DuckDB, вектор (FAISS).

ЗАПРЕЩЕНО импортировать сюда ``nanobot``, ``lib``, ``workspace``.
Проверяется ``tests/test_architecture_boundaries.py``.
"""
