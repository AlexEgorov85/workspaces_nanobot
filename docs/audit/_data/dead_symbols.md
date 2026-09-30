# Top-level symbols with zero references

Zero references means: no import of the name, no attribute use, no string
mention, in ANY file including tests. Strong dead-code candidates.

**18 symbols.**

| Symbol | Kind | LOC | Docstring |
|---|---|---:|---|
| `workspace/tools/example.py::ExampleTool` | class | 74 | Возвращает длину переданного текста. Шаблон для копирования. |
| `workspace/hooks/debug_stream_diag.py::StreamDiagnosisHook` | class | 55 |  |
| `workspace/utils/structure_cache.py::get_structure` | function | 45 | Получить структуру документа с кэшированием на диске. Повторные вызовы для того же файла ( |
| `lib/services/vector_index_service.py::VectorIndexBuildService` | class | 31 | Build-слой над общим провайдером кэша. Держит ОДИН экземпляр ``CacheProvider`` (не создаёт |
| `lib/core/bus_factory.py::build_logging_bus` | function | 27 | Синхронный shim: подменяет ``publish_outbound`` на запись в лог. Legacy-хелпер для сценари |
| `lib/core/skill_config.py::get_in_memory_cache_path` | function | 27 | Путь к файлу runtime-кэша (``cache.duckdb``). Файл общий для всех skill'ов. v2.5.2+ путь в |
| `workspace/utils/jsonb.py::decode_json_list` | function | 19 | Безопасно декодировать JSONB-список из БД в ``list``. Принимает: * ``None``/``""`` → ``[]` |
| `lib/core/skill_config.py::get_vector_index_path` | function | 18 | Путь к FAISS-индексу: ``<default_root>/<index_name>``. Берёт первый индекс из ``vector_ind |
| `workspace/utils/jsonb.py::decode_jsonb` | function | 16 | Безопасно декодировать JSONB-значение из БД в ``dict``. Принимает: * ``None`` → ``{}`` * ` |
| `lib/utils/outbound_meta.py::is_stream_delta` | function | 10 | True, если metadata содержит ``_stream_delta`` (чанк стрима). Legacy-проверка: в nanobot 0 |
| `config.py::get_active_profile` | function | 8 | Вернуть активный профиль (``SETTINGS["profile"]``). Бросает ``ConfigurationError``, если ` |
| `lib/services/runtime_inventory.py::diff_project_tools_from_detail` | function | 8 | Сравнить ``project_tools`` ``detail`` с каноническим списком. |
| `lib/core/skill_config.py::get_vector_indexes` | function | 7 | Метаданные индексов из ``gateway.vector.index.indexes`` (см. ``VectorIndexSettings.indexes |
| `lib/services/session_cold_sync_service.py::resolve_default_sqlite_path` | function | 6 | Дефолтный путь к SQLite-файлу ``LLMUsageStore`` из design D4. Используется в ``lib.service |
| `lib/core/skill_config.py::load_db_config` | function | 2 |  |
| `lib/core/skill_config.py::get_tool_config` | function | 2 |  |
| `lib/core/skill_config.py::get_embedding_model` | function | 2 |  |
| `workspace/skills/legal_summarizer/scripts/llm/config.py::get_timeout_sec` | function | 2 |  |
