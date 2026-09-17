## 1. Diagnostic helper в `cache/manifest.py`

- [ ] 1.1 Добавить `diagnose_manifest(operation_id, workspace_root) -> dict` в
      `workspace/skills/legal_summarizer/scripts/cache/manifest.py`. Возвращает
      `{"reason": "ok"|"not_found"|"corrupted"|"unsupported_version",
      "version_observed": int|None, "path": str, "raw": dict|None}`.
      Поля `version_observed`/`raw` заполняются только когда релевантны.
      `load_manifest()` не трогаем.
      Верификация: `python -c "from cache.manifest import diagnose_manifest;
      print(diagnose_manifest('no_such', '.'))"` возвращает
      `reason="not_found"`.

- [ ] 1.2 Покрыть `diagnose_manifest` unit-тестами на 4 случая:
      `manifest не существует`, `JSON повреждён`, `version=1`,
      `без поля version`. Различить два последних под
      `unsupported_version` с разным `version_observed` (для v1 — значение,
      для отсутствующего — `None`).
      Верификация: `pytest workspace/skills/legal_summarizer/tests/test_manifest_diagnose.py -v`
      зелёный.

## 2. CLI query: три `error_type` вместо одного

- [ ] 2.1 В `workspace/skills/legal_summarizer/scripts/cli_query.py` заменить
      `_load_manifest_or_none` на путь «сначала `diagnose_manifest`, при
      `reason != "ok"` — `_emit(...)` соответствующего error envelope и
      `return 1`». Envelope содержит `status="error"`, `error_type` равный
      `manifest_not_found` / `manifest_corrupted` /
      `manifest_unsupported_version`, `operation_id`, `path`, плюс
      `version_observed` где релевантно.
      Для `reason == "ok"` — продолжать через существующий
      `load_manifest().to_dict()`.
      Верификация: ручной прогон `python workspace/skills/legal_summarizer/scripts/cli_query.py --operation-id no_such --field stats`
      возвращает `error_type=manifest_not_found` и `exit=1`;
      на сломанном manifest — `error_type=manifest_corrupted` и `exit=1`;
      на manifest с `version=1` — `error_type=manifest_unsupported_version` и
      `exit=1`.

- [ ] 2.2 Добавить в `SKILL.md` явную секцию «IPC contract for follow-up
      queries» с таблицей `exit code` × `status` × `error_type`. Упомянуть
      `chunks_total` vs `field=chunks` как независимые источники.
      Верификация: `grep -n "IPC contract" workspace/skills/legal_summarizer/SKILL.md`
      находит секцию.

## 3. Wrapper: пробрасывать доменные ошибки

- [ ] 3.1 В `workspace/tools/legal_summarizer_query.py::execute` после
      `subprocess.run` изменить порядок: при `returncode != 0` сначала
      попытаться `json.loads(stdout)`; при успешном dict с полем `status` —
      вернуть как есть (`json.dumps(parsed, ensure_ascii=False)`).
      Только при пустом/невалидном stdout — текущий путь с `cli_failed`.
      Поведение success-пути (`returncode == 0`, валидный JSON,
      `empty_response`, `invalid_json`) не меняется.
      Верификация: ручной прогон wrapper через `python -c "from workspace.tools.legal_summarizer_query import LegalSummarizerQueryTool; ..."`
      на mock-объекте, имитирующем `returncode=1, stdout='{"status":"error","error_type":"manifest_not_found",...}'`,
      возвращает тот же JSON без обёртки `cli_failed`.

- [ ] 3.2 Существующие error-типы wrapper'а (`timeout`, `cli_not_found`,
      `subprocess_error`, `empty_response`, `invalid_json`,
      `cli_failed`) оставить в тех же code-paths. `cli_failed` теперь
      срабатывает **только** на невалидный stdout при non-zero exit.
      Верификация: `grep -n "error_type" workspace/tools/legal_summarizer_query.py`
      показывает все 6 строковых литералов без регрессии.

## 4. Регрессионные тесты

- [ ] 4.1 Добавить `tests/test_legal_summarizer_query_ipc.py` со
      сценариями: (a) `exit=0 + ok JSON` → wrapper возвращает payload;
      (b) `exit=1 + valid JSON error envelope` → wrapper возвращает
      тот же envelope без `cli_failed`; (c) `exit=1 + empty stdout` →
      wrapper возвращает `cli_failed`; (d) `exit=1 + invalid JSON stdout` →
      wrapper возвращает `cli_failed`; (e) три новых manifest-причины
      пробрасываются (`manifest_not_found` / `manifest_corrupted` /
      `manifest_unsupported_version`).
      Использовать `unittest.mock.patch` на `subprocess.run`.
      Верификация: `pytest tests/test_legal_summarizer_query_ipc.py -v`
      зелёный, 5+ сценариев.

- [ ] 4.2 Запустить полный набор `pytest` (включая существующие тесты
      `legal_summarizer` и `cache.manifest`); убедиться, что ничего не
      сломалось.
      Верификация: `pytest` завершается без новых failed/skipped.

## 5. Документация

- [ ] 5.1 Обновить docstring `workspace/tools/legal_summarizer_query.py`
      (секция «Контракт и поведение»): явно описать три режима IPC,
      перечислить доменные `error_type` и `cli_failed` как fallback.
      Верификация: `grep -n "exit 0\|exit != 0\|cli_failed" workspace/tools/legal_summarizer_query.py`
      находит описание.

- [ ] 5.2 В `CHANGELOG.md` под `## [Unreleased]` добавить категорию
      `Fixed`: «legal_summarizer_query: пробрасывает структурированные
      ошибки из cli_query.py (manifest_not_found / manifest_corrupted /
      manifest_unsupported_version) вместо обобщённого cli_failed».
      Верификация: `grep -n "manifest_unsupported_version" CHANGELOG.md`
      находит строку.

- [ ] 5.3 В `docs/skill-tool-architecture.md` (если там описан
      `legal_summarizer_query`) добавить короткий абзац про IPC-контракт
      skill↔tool. Если раздела нет — добавить ссылку на
      `workspace/skills/legal_summarizer/SKILL.md#ipc-contract`.
      Верификация: ссылка присутствует.
