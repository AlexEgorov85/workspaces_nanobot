## REMOVED Requirements

### Requirement: capability skills/audit-analyzer-query не вводится

Проектный tool `workspace/tools/audit_analyzer_query.py` как отдельная
точка доступа skill'а `audit_analyzer` к runtime-данным **не создаётся**.
Возможность покрывается существующим skill-side CLI-контуром.

#### Scenario: Агент обращается к данным audit_analyzer

- **WHEN** агенту требуется доступ к данным skill'а `audit_analyzer`
- **THEN** он использует skill-side CLI `scripts/cli.py --mode <predefined | generated_sql | vector>`
- **AND** отдельный Agent-facing tool для этих режимов MUST NOT существовать

#### Scenario: Граница skill ↔ storage-реализация

- **WHEN** skill обращается к SQL-кэшу или векторным индексам
- **THEN** обращение идёт через интерфейс `CacheProvider`
- **AND** skill MUST NOT знать конкретную storage-реализацию

## Обоснование отмены

1. Противоречит принятому эталону границы ответственности — **эпоха A/G,
   CLI-слой** (`docs/architecture/decisions/audit-analyzer-runtime-boundary.md`).
2. Противоречит нормативному документу `docs/skill-tool-architecture.md`:
   §1 — Skill не вызывает Tool программно; §8 — «Skill `audit_analyzer` —
   **CLI-only**».
3. Инъекция провайдера возможна только in-process, а skill исполняется в
   **subprocess** через `tools.exec`; заявленный механизм `set_provider` от
   `lib/services/project_tool_loader.py` в этом контуре не срабатывает —
   tool был бы мёртвым кодом.
