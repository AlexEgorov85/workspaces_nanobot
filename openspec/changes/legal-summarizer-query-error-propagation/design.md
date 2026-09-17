## Context

Текущая пара `workspace/tools/legal_summarizer_query.py` ↔
`workspace/skills/legal_summarizer/scripts/cli_query.py` имеет три
несвязанные проблемы в IPC-контракте и диагностике manifest (см.
`proposal.md` — Why). Этот документ фиксирует **как** именно они правятся:

1. Wrapper выбрасывает stdout при `returncode != 0`.
2. CLI схлопывает три причины `manifest is None` в один `error_type`.
3. `_read_json` в `cache/manifest.py` тоже не различает «нет файла» и
   «битый JSON» — оба возвращают `None`.

См. также `openspec/changes/.../specs/skills/legal-summarizer-query/spec.md`
для нормативного контракта.

## Goals / Non-Goals

**Goals:**

- Восстановить IPC-контракт «exit code + stdout JSON» так, чтобы wrapper
  пробрасывал доменную ошибку как есть и только при невалидном stdout
  откатывался к собственному `cli_failed`.
- Разделить три причины `manifest is None` на отдельные `error_type` без
  ломки существующего resume-пути.
- Сохранить нынешний успешный путь и существующие error-типы wrapper'а
  (`timeout`, `cli_not_found`, `subprocess_error`, `empty_response`,
  `invalid_json`) без изменения их семантики.

**Non-Goals:**

- Выравнивание `chunks_total` и `chunk_count` (фиксируется в спеке как
  допустимое расхождение, отдельный change если вообще нужно).
- Изменение `min/max` для `max_chunk_summary_chars`.
- Удаление `field=articles` или введение `--list-operations`.
- Изменение shape success-ответов, схемы параметров tool'а, формата
  manifest v2 (только диагностика, не формат).

## Decisions

### D1. Диагностическая функция в `cache/manifest.py` вместо ломки `load_manifest`

**Решение:** добавить новую функцию `diagnose_manifest(operation_id,
workspace_root) -> dict` рядом с существующим `load_manifest()`,
которая возвращает словарь с полями:

- `reason`: `"ok"` / `"not_found"` / `"corrupted"` /
  `"unsupported_version"`;
- `path`: абсолютный путь к manifest-файлу (для диагностического
  сообщения);
- `version_observed`: `int | None`. Заполняется только когда
  `reason == "ok"` или `reason == "unsupported_version"`. Значение
  берётся из `manifest.version`, **только если оно может быть прочитано
  как int**; во всех остальных случаях (поле отсутствует, `"abc"`,
  массив и т.п.) — `None`. Это убирает неоднозначность: `None` означает
  «не удалось проинтерпретировать как int», а не «поля не было».

`raw` (содержимое manifest) **не возвращается**: для `corrupted`
получить его невозможно, для остальных случаев CLI он не нужен для
формирования error envelope. Существующий `load_manifest()` остаётся без
изменений (резюм-путь использует его как есть и не нуждается в различии
причин).

**Альтернативы:**

- Изменить `load_manifest` чтобы он возвращал `(manifest, reason)` —
  ломает сигнатуру, требует правки всех вызовов в резюм-пути.
  Отвергнуто: больше шанс регрессии, не даёт выигрыша для текущего
  scope.
- Бросать исключения вместо возврата `None` — ломает контракт для
  существующих вызовов и плохо сочетается с «отсутствие = норма» в
  resume-пути.
- Использовать sentinel-класс `ManifestUnavailable(reason)` — over-engineering
  для одного CLI.

### D2. Чтение manifest в два шага для `cli_query.py`

**Решение:** в `cli_query.py._load_manifest_or_none` (или заменить её на
две функции) перед нормализацией вызывать `diagnose_manifest`, и если
`reason != "ok"` — сразу эмитить JSON-ошибку с нужным `error_type`
(`manifest_not_found` / `manifest_corrupted` /
`manifest_unsupported_version`) и `return 1`. Иначе — продолжить
текущий путь через `load_manifest()`.

**Альтернативы:**

- Добавить `error_type` параметр в `load_manifest` — плохая связность,
  loader не должен знать про IPC-протокол CLI.
- Делать диагностику в самом `cli_query.py` через прямой `_read_json` +
  `_detect_version` — дублирует логику loader'а и расходится с ним при
  правках.

### D3. Сначала парсить stdout при non-zero exit, со строгим `status == "error"`

**Решение:** в `legal_summarizer_query.py.execute` после `subprocess.run`
при `returncode != 0` сначала попытаться распарсить stdout как JSON.
Если stdout — JSON-объект, у которого top-level `status == "error"` —
вернуть его как JSON-строку без модификаций (все поля сохранены).
В **любом другом** случае (stdout пустой, stdout не JSON,
stdout — JSON-массив, JSON-объект без поля `status`, JSON-объект с
`status != "error"`) — вернуть собственный envelope `cli_failed` со
stderr-фрагментом. Это сужает «проброс» строго до доменной ошибки и
защищает от случайного `exit 1 + {"status":"ok"}` или
`exit 1 + []`.

Конкретный поток:

```text
if returncode != 0:
    parsed = safe_json(stdout)
    if (
        parsed is not None
        and isinstance(parsed, dict)
        and parsed.get("status") == "error"
    ):
        return json.dumps(parsed, ensure_ascii=False)
    return self._error("cli_failed", ...)

stdout = stdout.strip()
if not stdout:
    return self._error("empty_response", ...)

try:
    payload = json.loads(stdout)
except JSONDecodeError:
    return self._error("invalid_json", ...)
```

**Альтернативы:**

- Оставить прежний порядок (returncode → stdout) — текущий баг.
- Принимать любой `status in parsed` без проверки значения — поймает
  `{"status":"ok"}` на non-zero exit как success, что нарушает контракт.
- Парсить stdout **всегда** и игнорировать `returncode` полностью —
  плохо: настоящий process failure (segfault, OOM kill) не имеет
  structured JSON, и мы теряем сигнал «CLI вообще упал».

### D4. Не вводить поле `error_type` дополнительно к доменным + pass-through полей

**Решение:** доменные ошибки (`manifest_not_found` и т.п.) уже имеют
`error_type` в stdout JSON от CLI — wrapper просто пробрасывает поле
как есть. Не нужно вводить второе поле вроде `wrapper_status` или
`tool_status`. Существующий `_error()` хелпер wrapper'а используется
только для настоящих wrapper-уровневых ошибок (`cli_failed`,
`timeout`, `cli_not_found`, `subprocess_error`, `empty_response`,
`invalid_json`).

Pass-through означает: wrapper сериализует CLI-envelope **as is**, не
фильтруя поля. `operation_id`, `version_observed`, `path`, `message` и
любые будущие поля доходят до агента без переименования и обёртки.

**Альтернативы:**

- Помечать пробрасываемый `error_type` префиксом (например
  `cli_manifest_not_found`) — нарушает нормативный контракт спеки, где
  перечислены доменные `error_type` напрямую.

### D5. Документация IPC в `SKILL.md`, без изменения контракта skill'а

**Решение:** добавить секцию «IPC contract for follow-up queries» в
`workspace/skills/legal_summarizer/SKILL.md`, явно перечисляющую три
режима (success / domain error / process failure) и таблицу
`error_type`. Существующие секции (manifest v2 schema, brief
семантика) не трогаем.

**Альтернативы:**

- Вынести IPC-контракт в отдельный `docs/legal_summarizer_ipc.md` —
  избыточно для одного skill'а; `SKILL.md` уже описывает read-only
  контракт.

## Risks / Trade-offs

- **[Дополнительный I/O в CLI]**: `cli_query.py` теперь делает два
  обращения к manifest (диагностика + нормализация). На локальной FS это
  +1 read, ниже микросекунды, несущественно. Оптимизация чтения —
  отдельная задача, **не входит** в этот change.
- **[Рост surface у `_detect_version`]**: сейчас возвращает `None` или
  `MANIFEST_VERSION_V2`. Диагностическая функция должна различить
  «нет version field» и «version != 2» — формально это два разных
  исхода для пользователя. → Митигация: тесты на manifest с
  `version=1`, manifest без version, manifest с `version="abc"`.
- **[Изменение `error_type` поведения существующих клиентов]**:
  если кто-то за пределами этого репо парсил вывод wrapper'а
  по `error_type == "cli_failed"` как «manifest не найден», это
  сломается. → Митигация: `cli_failed` остаётся как fallback для
  невалидного stdout, не убирается. Реальных внешних клиентов нет.
- **[Backward-incompatible manifest diagnostics]**: до этого change
  CLI всегда возвращал `manifest_not_found` независимо от причины.
  Любой существующий интеграционный тест на `manifest_not_found`
  должен пройти по-прежнему на сценарии «файла нет». Тесты на
  corrupted/unsupported_version — новые, должны быть добавлены. →
  Митигация: tasks §3 содержит явный пункт на регрессионный тест
  «отсутствующий manifest → manifest_not_found».

## Migration Plan

Не применимо в полном смысле:

- Никаких миграций БД.
- Никаких изменений конфига.
- Никаких изменений CLI-аргументов `cli_query.py` (те же `--operation-id`,
  `--field`, `--workspace-root`, `--max-chunk-summary-chars`).
- Никаких изменений tool schema (те же `operation_id`, `field`,
  `max_chunk_summary_chars`).

**Deployment:**

1. Смержить change в `master`.
2. Воркер-пул/CLI/gateway подхватывают новые `error_type` без
   перезапуска (всё в Python, нет кешей схем).
3. Smoke: запустить существующие тесты + добавить новые (tasks §3).

**Rollback:**

- Revert PR. Поведение `cli_failed` восстанавливается как было.
  Никаких persistent-эффектов нет.

## Open Questions

Нет. Все потенциальные вопросы либо уже разрешены в proposal/specs
(`chunks_total` vs `chunks` — документируем, не правим; `articles` —
оставляем; `--list-operations` — нет; `max_chunk_summary_chars` min —
оставляем), либо тривиальны и могут быть решены в design phase tasks.
