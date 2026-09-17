## Why

Сейчас tool `legal_summarizer_query` при любом `returncode != 0` от
`workspace/skills/legal_summarizer/scripts/cli_query.py` немедленно возвращает
собственный `cli_failed`, не читая stdout. CLI при этом печатает в stdout
структурированную JSON-ошибку (`status=error`, `error_type=manifest_not_found`,
`operation_id`, `message`) и `return 1`. Tool теряет всю диагностику и
подменяет её обобщённым `cli_failed` со stderr-фрагментом. Это нарушает
IPC-контракт subprocess-boundary Tool↔CLI и снижает качество tool-ответа:
агент видит «cli вернул exit=1», а не «manifest не найден / повреждён /
неподдерживаемая версия». Дополнительно `cache.manifest.load_manifest()`
схлопывает три разные причины (`file missing`, `json corrupted`,
`version != 2`) в один `None`, который CLI раскрывает одним
`manifest_not_found` — двойное схлопывание диагностической информации.

## What Changes

- В `cli_query.py` при `manifest is None` **перед** `return 1` tool/tooling
  читает manifest через отдельный диагностический путь, различающий
  `manifest_not_found`, `manifest_corrupted`, `manifest_unsupported_version`.
  Каждая причина формирует собственный `error_type` в stdout JSON.
- В `legal_summarizer_query.py` wrapper при `returncode != 0` **сначала**
  пытается распарсить stdout как JSON; если это валидный
  `{"status": "error", ...}` — пробрасывает структурированную ошибку как
  есть (`error_type`, `message`, дополнительные поля). Только если stdout
  пустой или невалидный JSON — возвращает `cli_failed` как
  действительно-непредвиденный-сбой-CLI.
- Параллельно вводится явный формальный IPC-контракт CLI:
  `exit 0 + valid JSON → success`, `exit != 0 + valid JSON → domain error`,
  `exit != 0 + empty/invalid JSON → process failure`. Контракт документируется
  в docstring обоих файлов и в `workspace/skills/legal_summarizer/SKILL.md`.
- Поведение success-пути и schema (`field` enum, `max_chunk_summary_chars`,
  error-taxonomy `timeout`/`cli_not_found`/`subprocess_error`/`empty_response`/
  `invalid_json`) не меняется.
- `field=chunks` vs `chunks_total`: семантическая разница (физические partial
  results vs логический счётчик) **не правится кодом в этом change**.
  Это констатируется в proposal как известное расхождение и описывается в
  `SKILL.md` как явный контракт. Любая попытка выровнять значения — отдельный
  change с предварительным исследованием brief-семантики.
- `--list-operations`, изменение `min` для `max_chunk_summary_chars`,
  удаление `field=articles` — **не входят** в этот change (не доказано как
  дефект).

**Schema и success-path публичного tool API не меняются.** `field` enum,
`max_chunk_summary_chars` range, shape success-ответов, формат manifest v2 —
всё остаётся прежним. Error contract tool'а расширяется: ранее скрытые
доменные ошибки (`manifest_not_found` / `manifest_corrupted` /
`manifest_unsupported_version`) становятся доступны вызывающему коду как
отдельные `error_type`. Раньше все они схлопывались в обобщённый
`cli_failed` (потому что wrapper выбрасывал stdout при non-zero exit).
Сам `cli_failed` остаётся как fallback для настоящего process failure
(невалидный stdout, неожиданный JSON-формат).

## Capabilities

### New Capabilities

- `skills/legal-summarizer-query`: read-only follow-up запросы к skill
  `legal_summarizer` через subprocess-boundary. Контракт:
  IPC-протокол CLI (`exit code` ↔ `status` поля), структура
  manifest-диагностики, и поведение wrapper при success / domain error /
  process failure.

### Modified Capabilities

Нет. Существующие спеки (`architecture/skill-tool-boundary`, `data/cache`,
`runtime/context`, `configuration/profiles`, `data/vector-indexes`) не
затрагиваются на уровне requirements: ни domain-vs-infrastructure граница,
ни cache-контракт, ни runtime-профиль не меняются. IPC-контракт между
tool и CLI skill'а — локальная ответственность самого skill/tool pair.

## Impact

- `workspace/tools/legal_summarizer_query.py` — wrapper при `returncode != 0`.
- `workspace/skills/legal_summarizer/scripts/cli_query.py` — диагностика
  причин `manifest is None` через раздельный loader.
- `workspace/skills/legal_summarizer/scripts/cache/manifest.py` — экспорт
  новой диагностической функции `diagnose_manifest(operation_id,
  workspace_root) -> dict` с полями `reason` / `path` /
  `version_observed` (без `raw`), без ломки существующего
  `load_manifest()` (resume-протокол продолжает использовать
  неразличающий API).
- `workspace/skills/legal_summarizer/SKILL.md` — фиксация IPC-контракта и
  явное описание `chunks_total` vs `field=chunks`.
- `tests/` — добавить unit-тесты для новых `error_type` и для пути
  `exit != 0 + invalid JSON → cli_failed` (поведение не должно сломаться).
- Зависимостей, миграций БД, изменений публичного API вне пары
  `legal_summarizer_query` ↔ `cli_query.py` нет.
