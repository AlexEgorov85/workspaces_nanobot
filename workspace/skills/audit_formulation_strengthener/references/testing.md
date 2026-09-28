# Тестирование skill'а `audit_formulation_strengthener`

## Философия

- **Моки только на границе `scripts.llm.chat_json/chat`.** LLM-вызовы замоканы
  ровно в одной точке — тонкой обёртке над `lib.services.llm_client.call_llm`.
  Это даёт честные unit-тесты режимов без сети и без зависимости от
  внутренней структуры `lib.services.llm_client`.
- **VND через `extract_text` замоканы в `prepare_vnd`** (или используются реальные
  tmp-файлы — `extract_text` для `.txt` не требует внешних библиотек типа pypdf).
- **Архитектурные инварианты проверяются в `test_architecture.py`** через AST
  и plain string по `scripts/**/*.py`. Это страховка от регрессии, если кто-то
  добавит импорт `legal_summarizer` или мутацию `sys.path` мимо `cli.py`.
- **Никаких реальных сетевых вызовов.** Все LLM-ответы — захардкоженные dict'ы.

## Структура

```
tests/
├── conftest.py              # REPO_ROOT в sys.path; фикстуры mock_llm_*, sample_vnd_files
├── test_architecture.py     # AST + plain string по scripts/**
├── test_helpers.py          # load_prompt, render_prompt, make_error, prepare_output
├── test_mode_analyze.py     # все сценарии analyze
├── test_mode_search.py      # все сценарии search
├── test_mode_synthesize.py  # все сценарии synthesize
└── test_cli.py              # end-to-end через main(argv)
```

## Mock-граница: `scripts.llm.chat_json` / `chat`

```python
import workspace.skills.audit_formulation_strengthener.scripts.llm as llm_mod

def fake_chat_json(*, system, user, operation):
    return {
        "analyze_in_progress": ...,
    }[operation]

monkeypatch.setattr(llm_mod, "chat_json", fake_chat_json)
```

Фикстура возвращает dict по `operation` (`"analyze"` / `"search_map"` /
`"synthesize"`), не парсит текст промпта. Это исключает хрупкие
тесты, привязанные к формулировкам промптов.

## Обязательные сценарии

### analyze

- success — happy path с минимальным валидным LLM-ответом.
- empty violation → `empty_violation` + exit 2.
- invalid severity (`"критическая"`) → coerce в `"средняя"`.
- key_concepts содержит int/float/None → фильтр оставляет только str.
- JSON-мусор обе попытки → `json_parse_failed`.
- Schema mismatch (нет `normalized`) → `schema_mismatch`.
- estimate-only без vnd → 0 LLM-вызовов, форма `{estimate:true, violation_chars, vnd_count, llm_calls_planned:1}`.

### search

- map+фильтр+top-K порядок: чанки сортируются по `relevance_score` убывание, берётся top-10.
- Частичный отказ (1 из 3 чанков упал) → `chunks_failed=1`, status=`success`, находки от оставшихся.
- Все чанки упали → `status=error`, `error_type=llm_error`.
- evidence_id назначаются последовательно F1..FN.
- Пустой файл → `vnd_empty` с именем файла в сообщении.
- Превышение лимита → `too_many_chunks`.
- Нечитаемый analyze-файл → `analyze_result_unreadable`.
- Без `--analyze-result` и без `--analyze-result-path` → fallback на сырой `violation`.
- estimate-only → 0 LLM-вызовов, форма `{estimate:true, size_estimate, llm_calls_planned:N}`.

### synthesize

- Валидная подстрока → проходит, попадает в `vnd_citations`.
- Выдуманный excerpt (правленый) → отбрасывается, `citations_dropped` инкрементируется.
- Несуществующий evidence_id (например, F99) → отбрасывается.
- `relation_type` берётся из находки, не из LLM (даже если LLM вернул иной).
- Markdown 6 секций + blockquote для цитат + meta-строка с датой и числом evidence.
- Plain text без markdown-разметки (без `##`, без `**`, без `>`).
- DOCX (skipif без python-docx).
- Все цитаты отброшены → status=`success` (не error), в отчёте плейсхолдер.
- Resume из файлов `--analyze-result` + `--search-result`.
- estimate-only → parse+chunk ВНД ради N, 0 LLM-вызовов, форма `{estimate:true, violation_chars, size_estimate, llm_calls_planned:1+N+1}`.

### cli (end-to-end через `main(argv)`)

- `--mode all --estimate-only` → 0 LLM-вызовов, exit 0.
- Без `--vnd` (для search/synthesize/all) → exit 2, JSON `no_vnd` в stdout.
- Несуществующий файл → exit 2, JSON `vnd_not_found`.
- Пустой violation → exit 2, JSON `empty_violation`.
- `--output /tmp/x.md` + synthesize → файл создан И JSON `{mode, status, saved_to}` в stdout.
- `--estimate-only + --output` → `--output` игнорируется, файл не создаётся, JSON в stdout.
- `--internal-format json` → JSON в stdout для synthesize.
- Exit-коды для всех error_type (parametrize).

### helpers

- `load_prompt` существующего файла → содержимое.
- `load_prompt` несуществующего → `FileNotFoundError`.
- `render_prompt` с полным набором vars → подставляет.
- `render_prompt` без обязательного ключа → `PromptUnresolvedVarError` с `unresolved=[...]`.
- `make_error` без error_type → dict без поля `error_type`.
- `prepare_output` с `{status, data}` → нормализует и оборачивает в `{mode, status, data}`.

### architecture

- `legal_summarizer` нигде в `scripts/**/*.py` (включая docstring и комментарии — `grep -rIn 'legal_summarizer' scripts/` = 0).
- `sys.path` мутируется только в `cli.py`.
- 0 импортов `workspace.tools` в `scripts/**/*.py`.

## Прогон

```bash
cd workspace/skills/audit_formulation_strengthener
python -m pytest
```

`pytest.ini` уже содержит `testpaths = tests` и `--strict-markers`.

## Smoke (без сети)

```bash
python workspace/skills/audit_formulation_strengthener/scripts/cli.py \
    --mode all --estimate-only \
    --violation "Срок хранения ПДн установлен 1 год" \
    --vnd /tmp/sample.txt
```

Ожидаемый результат: exit 0, JSON с `estimate: true`, `size_estimate`, `llm_calls_planned: 1+N+1`, 0 LLM-вызовов.
