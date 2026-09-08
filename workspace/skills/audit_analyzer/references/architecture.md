# Audit Analyzer Architecture

## Core tools

Skill использует только два инструмента получения данных:

- `duckdb_query`
- `vector_search`

Оба инструмента являются generic capabilities.

Они не знают ничего про audit_analyzer.

## Agent responsibility

Agent:

1. читает SKILL.md;
2. определяет способ получения данных;
3. формирует параметры tool;
4. вызывает tool;
5. анализирует результат;
6. при необходимости повторяет вызов;
7. формирует ответ пользователю.

## Skill responsibility

Skill содержит:

- предметные знания;
- описание данных;
- правила выбора способа получения данных;
- каталог predefined SQL (имена, параметры, назначение);
- описания vector indexes;
- SQL guidance для генерации свободных запросов;
- ограничения;
- примеры;
- skill-side helper `scripts/sql_generator.py` для генерации SQL по
  NL-запросу через прямой LLM API-вызов (если Agent предпочитает
  не генерировать SQL сам).

## Script responsibility

Python scripts разрешены для:

- самостоятельной детерминированной обработки;
- skill-side LLM-генерации SQL (через прямой API-вызов).

Python script не должен существовать только ради вызова другого tool.

## Dependency direction

Core не импортирует audit_analyzer.

audit_analyzer использует generic capabilities core:

- `duckdb_query` (workspace/tools/duckdb_query_tool.py);
- `vector_search` (workspace/tools/vector_search_tool.py);
- `lib.services.llm_client.call_llm` (если нужен LLM-вызов);
- `lib.utils.sql_safety.validate_sql`;
- `lib.utils.text_utils.{sanitize_value, truncate_middle}`;
- `lib.core.skill_config.get_predefined_scripts_table`.

## Forbidden

Запрещены:

- audit-specific tools в core;
- `run_predefined_script` как отдельный tool;
- `nl_sql_generate` как отдельный tool;
- Python wrappers вокруг `duckdb_query`;
- Python wrappers вокруг `vector_search`;
- audit-specific SQL routing в core;
- audit-specific prompts в core.

Допустимы:

- `public.agent_predefined_scripts` как PG-источник SQL для Agent;
  Agent читает SQL inline через `duckdb_query` (см.
  `references/predefined_scripts.md`);
- skill-side `scripts/sql_generator.py` для автономной генерации SQL
  через прямой LLM API-вызов (используется по желанию Agent'а).
