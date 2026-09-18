## 1. Diagnostic helper в `cache/manifest.py`

- [x] 1.1 Добавить `diagnose_manifest(operation_id, workspace_root) -> dict` в
      `workspace/skills/legal_summarizer/scripts/cache/manifest.py`. Возвращает
      `{"reason": "ok"|"not_found"|"corrupted"|"unsupported_version",
      "version_observed": int|None, "path": str}`.
      `version_observed` заполняется только когда `reason == "ok"` или
      `reason == "unsupported_version"` и значение удалось прочитать.
      `raw` намеренно **не возвращается**: для `corrupted` его получить
      невозможно, для остальных случаев CLI он не нужен.
      `load_manifest()` не трогаем.
      Верификация: `python -c "from cache.manifest import diagnose_manifest;
      print(diagnose_manifest('no_such', '.'))"` возвращает
      `reason="not_found"` и **не** содержит поля `raw`.

- [x] 1.2 Покрыть `diagnose_manifest` unit-тестами на 5 случаев:
      `manifest не существует`, `JSON повреждён`, `version=1`,
      `без поля version`, `version="abc"` (non-integer). Различить под
      `unsupported_version`: для `version=1` → `version_observed=1`,
      для отсутствующего и для `version="abc"` →
      `version_observed=None`.
      Верификация: `pytest workspace/skills/legal_summarizer/tests/test_manifest_diagnose.py -v`
      зелёный, 5+ кейсов.

## 2. CLI query: три `error_type` вместо одного

- [x] 2.1 В `workspace/skills/legal_summarizer/scripts/cli_query.py` заменить
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

- [x] 2.2 Добавить в `SKILL.md` явную секцию «IPC contract for follow-up
      queries» с таблицей `exit code` × `status` × `error_type`. Упомянуть
      `chunks_total` vs `field=chunks` как независимые источники.
      Верификация: `grep -n "IPC contract" workspace/skills/legal_summarizer/SKILL.md`
      находит секцию.

## 3. Wrapper: пробрасывать доменные ошибки

- [x] 3.1 В `workspace/tools/legal_summarizer_query.py::execute` после
      `subprocess.run` изменить порядок: при `returncode != 0` сначала
      попытаться `json.loads(stdout)`; пробросить JSON-строку только если
      результат — dict с `status == "error"` (строгое равенство значения,
      а не просто наличие поля). Все остальные случаи (stdout пустой,
      stdout не JSON, stdout — JSON-массив, dict без `status`,
      dict со `status != "error"`) идут в собственный `cli_failed`.
      Поведение success-пути (`returncode == 0`, валидный JSON,
      `empty_response`, `invalid_json`) не меняется.
      Верификация: ручной прогон wrapper через
      `python -c "from workspace.tools.legal_summarizer_query import LegalSummarizerQueryTool; ..."`
      на mock-объекте, имитирующем
      `returncode=1, stdout='{"status":"error","error_type":"manifest_not_found",...}'`,
      возвращает тот же JSON без обёртки `cli_failed`. Mock на
      `returncode=1, stdout='{"status":"ok"}'` возвращает `cli_failed`.

- [x] 3.2 Существующие error-типы wrapper'а (`timeout`, `cli_not_found`,
      `subprocess_error`, `empty_response`, `invalid_json`,
      `cli_failed`) оставить в тех же code-paths. `cli_failed` теперь
      срабатывает **только** на невалидный/неожиданный stdout при
      non-zero exit.
      Верификация: `grep -n "error_type" workspace/tools/legal_summarizer_query.py`
      показывает все 6 строковых литералов без регрессии.

## 4. Регрессионные тесты

- [x] 4.1 Добавить `tests/test_legal_summarizer_query_ipc.py` со
      сценариями (через `unittest.mock.patch` на `subprocess.run`):
      (a) `exit=0 + {"status":"ok",...}` → wrapper возвращает payload
      без обёртки;
      (b) `exit=1 + {"status":"error","error_type":"manifest_not_found",...}`
      → wrapper возвращает тот же envelope без `cli_failed`;
      (c) `exit=1 + stdout=""` → wrapper возвращает `cli_failed`;
      (d) `exit=1 + stdout="not json"` → wrapper возвращает `cli_failed`;
      (e) `exit=1 + stdout="[]"` (валидный JSON-массив) → wrapper
      возвращает `cli_failed`;
      (f) `exit=1 + stdout='{"status":"ok"}'` → wrapper возвращает
      `cli_failed` (строгий status-check, не просто наличие поля);
      (g) `exit=1 + stdout='{"foo":"bar"}'` (dict без `status`) →
      wrapper возвращает `cli_failed`;
      (h) три новых manifest-причины пробрасываются
      (`manifest_not_found` / `manifest_corrupted` /
      `manifest_unsupported_version`) — каждый как отдельный кейс
      с реальным файлом во временной директории;
      (i) pass-through полей: при `exit=1 + {"status":"error",
      "error_type":"manifest_unsupported_version", "operation_id":"abc",
      "version_observed":1, "path":"..."}` итоговый JSON-string
      содержит все 5 полей с исходными значениями (точное сравнение
      `json.loads(result)`).
      Верификация:
      * `pytest tests/test_legal_summarizer_query_ipc.py -v` зелёный,
        9+ unit-сценариев (a–g, h через мок pass-through, i);
      * `pytest tests/test_legal_summarizer_query_manifest_integration.py -v`
        зелёный, 9 интеграционных сценариев (manifest на диске → реальный
        `cli_query.py` subprocess → для 6 кейсов проверяется CLI-envelope
        и `exit=1`; для 3 кейсов проверяется **полный путь** wrapper
        pass-through без `cli_failed`).
      NOTE: (h) разнесён на две реализации — мок-pass-through (ipc-юнит)
      и **реальный** полный IPC-путь (manifest_integration). Это
      закрывает acceptance criterion дословно: «каждый как отдельный
      кейс с реальным файлом во временной директории».

- [x] 4.2 Запустить полный набор `pytest` (включая существующие тесты
      `legal_summarizer` и `cache.manifest`); убедиться, что ничего не
      сломалось.
      Верификация (прогон на HEAD):
      * **scope-зонa** (419 тестов за 3.85s, 100% зелёных):
        `workspace/skills/legal_summarizer/tests/test_manifest_diagnose.py`,
        `tests/test_legal_summarizer_query_ipc.py`,
        `tests/test_legal_summarizer_query_manifest_integration.py`,
        `workspace/skills/legal_summarizer/tests/architecture/`,
        `tests/test_skill_tool_independence.py`,
        `tests/test_architecture_tool_domain_free.py`,
        `tests/test_manifest.py`, `tests/test_config_keys.py`,
        `tests/test_project_settings.py`.
      * **full suite** (tests/ + workspace/skills/legal_summarizer/tests/,
        HEAD = ``4332cc0``):
        3046 passed, 16 skipped, 1 xfailed, 9 failed in 02:57.
        8 из 9 failures — pre-existing долг, воспроизводится на чистом
        master (отсутствие ``output.presenter`` в 6 кейсах
        ``test_skill_legal_summarizer.py::*_output_*``,
        ``test_context_immutability.py::test_ctx_chunks_immutable``,
        ``test_recovered_invariants.py::test_presenter_*``).
        1 failure — flaky ``test_build_vectors_cli.py::test_validate_only_flag_in_cli``
        (race между логированием DB-connect fail и матчем по строке);
        проходит изолированно — не регрессия change.
      * Полные логи прогонов сохранены локально и могут быть воспроизведены
        любой стороной через ``python -m pytest tests/ workspace/skills/legal_summarizer/tests/``.
      * Логи прошлого прогона (артефакты независимой верификации):
        ``<tmp>/relfix-pytest/full-YYYYMMDD-HHMMSS.log`` — 3046 passed,
        9 failed;
        ``<tmp>/relfix-pytest/master-YYYYMMDD-HHMMSS.log`` —
        ``test_context_immutability.py::test_ctx_chunks_immutable_after_estimate_and_execution``
        воспроизводится и на чистом master (подтверждает pre-existing долг);
        ``<tmp>/relfix-pytest/scope-YYYYMMDD-HHMMSS.log`` — 419 passed
        в scope-зоне change.

## 5. Документация

- [x] 5.1 Обновить docstring `workspace/tools/legal_summarizer_query.py`
      (секция «Контракт и поведение»): явно описать три режима IPC,
      перечислить доменные `error_type` и `cli_failed` как fallback.
      Верификация: `grep -n "exit 0\|exit != 0\|cli_failed" workspace/tools/legal_summarizer_query.py`
      находит описание.

- [x] 5.2 В `docs/skill-tool-architecture.md` (если там описан
      `legal_summarizer_query`) добавить короткий абзац про IPC-контракт
      skill↔tool. Если раздела нет — добавить ссылку на
      `workspace/skills/legal_summarizer/SKILL.md#ipc-contract`.
      Верификация: ссылка присутствует; также добавлена ссылка на
      новый интеграционный тест
      `tests/test_legal_summarizer_query_manifest_integration.py`.
