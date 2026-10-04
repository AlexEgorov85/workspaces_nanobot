# storage/session-recovery Specification

## Purpose
Lets the runtime safely recover sessions whose JSONL source is older than the PostgreSQL cold mirror (volume restore, host migration, multi-instance misconfiguration). Provides explicit, opt-in recovery modes so the gateway never silently overwrites authoritative cold-storage data.

## Scope

`agent` — подсистема **частично реализована**: из семи требований закрыто одно.

Реализовано (как инфраструктура `SessionMirror`,
`lib/gateway/mirror/`):
- «Reverse sync lag produces a logged event» — порог
  `sync_lag_threshold_seconds` (дефолт 3600), событие
  `sync_lag_exceeded`, счётчик `sync_lag_exceeded_total`;
- смежный stale-detection: `stale_tolerance_seconds` (дефолт 120), событие
  `session_stale_detected`, счётчики `stale_detected_total` и
  `stale_sync_skipped_total`. В самой спеке это требование не выделено, но
  детект — фундамент всех остальных.

Не реализовано ни в одном дереве:
- три режима восстановления (`detect-only`, `read-only-fallback`,
  `backup-and-restore`) — в `config.json` нет ни одного из них;
- read-only маркер сессии и запрет `save()` по нему;
- backup-and-restore с атомарным swap и copy-and-unlink на разных ФС;
- ограничение размера восстановления (100 МБ) и ротация бэкапов;
- `tools/recover_stale_sessions.py` — файла нет.

То есть спека описывает несуществующий **слой действий** поверх уже
работающего слоя **детектов**.

**Как это произошло.** Change `2026-09-27-session-recovery` лежит в
`openspec/changes/archive/` со всеми задачами, отмеченными `[x]`, включая
«Написать admin tool `tools/recover_stale_sessions.py`» и «`SessionRecoveryService`
создан в `ApplicationContext.create()`». Ни того, ни другого в репозитории нет:
`git log --all -- tools/recover_stale_sessions.py` пуст — файл не был
закоммичен ни разу, то есть change заархивирован как выполненный без кода.

Следствие для читателя спеки: не считать её требования действующими. До
появления реализации (отдельный change-каталог) это нормативное описание
намерения, у которого нет ни одной точки исполнения, кроме перечисленного
выше детекта.

## Requirements

### Requirement: Session recovery is opt-in via explicit mode
The system SHALL support exactly three recovery modes selected by configuration: `detect-only`, `read-only-fallback`, `backup-and-restore`. The system SHALL NOT mutate any session JSONL file while mode is `detect-only` or `read-only-fallback`. In mode `read-only-fallback` the system SHALL return a session object sourced from PostgreSQL without writing to the JSONL slot, and SHALL mark the returned object as read-only.

#### Scenario: Default mode is detect-only
- **WHEN** the runtime starts without an explicit recovery mode override
- **THEN** stale sessions are detected and logged but no JSONL file is written or replaced
- **AND** no session object is returned from the recovery service

#### Scenario: Read-only-fallback returns PG materialisation
- **WHEN** the runtime starts with mode `read-only-fallback` and a stale session is observed
- **THEN** the recovery service returns a session object reconstructed from PostgreSQL
- **AND** the JSONL slot for that session is not written
- **AND** the returned object is marked so subsequent save attempts are rejected

#### Scenario: Mode change requires restart
- **WHEN** the operator changes the recovery mode in configuration
- **THEN** the new mode takes effect only after the next gateway restart
- **AND** no in-process mode switch occurs

### Requirement: Read-only mode forbids any write to the stale session
The system SHALL refuse any save attempt against a session marked as recovered read-only. The system SHALL raise an error that names the session key and the required mode to escape read-only state. The read-only marker SHALL survive metadata mutations performed by upstream session helpers (timestamp updates, system field additions) until the marker is explicitly cleared.

#### Scenario: Save attempt against read-only session is rejected
- **WHEN** upstream code calls `save()` on a session whose metadata marks it read-only
- **THEN** the call is rejected with a clear error
- **AND** no JSONL or cold-storage write is performed

#### Scenario: Upstream metadata mutation does not clear the marker
- **WHEN** upstream code mutates the session metadata while leaving the read-only marker intact
- **THEN** the next `save()` attempt is still rejected

### Requirement: Backup-and-restore performs atomic swap
The system SHALL preserve the original JSONL file before overwriting it from PostgreSQL. The system SHALL ensure that a crash, rollback, or filesystem boundary issue never leaves the JSONL slot empty. The system SHALL store backup artifacts in a directory that is not the same directory as the JSONL slots.

#### Scenario: Successful restore
- **WHEN** the operator runs `backup-and-restore` against a stale session
- **THEN** the original JSONL is moved to a backup location that is not the JSONL directory
- **AND** the JSONL slot is rewritten from PostgreSQL
- **AND** a backup artifact exists on disk

#### Scenario: Rollback on failure
- **WHEN** any step of the restore cannot be completed
- **THEN** the original JSONL is restored from the backup location
- **AND** no partial or empty JSONL is observed by upstream code

#### Scenario: Cross-filesystem rename handled
- **WHEN** the source volume and the backup volume are on different filesystems
- **THEN** the system falls back to a copy-and-unlink strategy
- **AND** the original slot is never empty between steps

#### Scenario: Concurrent cold-sync during restore
- **WHEN** the cold-sync background loop observes the JSONL slot while restore is in progress
- **THEN** the cold-sync loop does not raise an unhandled error and does not write to the cold mirror
- **AND** on the next iteration after the restore completes the JSONL and cold mirror are consistent

### Requirement: Restore size is bounded
The system SHALL refuse to materialize a session whose total message content exceeds 100 MB.

#### Scenario: Oversized session is rejected
- **WHEN** the cold mirror contains a session whose combined message payload exceeds 100 MB
- **THEN** the restore is aborted with a clear error that names the session key and the measured size
- **AND** no JSONL file is written

### Requirement: Backup rotation is multi-instance safe
The system SHALL prune old backup files using age and per-session count limits, and SHALL tolerate a backup file being concurrently removed by another instance.

#### Scenario: Concurrent removal does not raise
- **WHEN** another gateway instance removes a backup file between listing and deletion
- **THEN** the rotation step records the absence and continues
- **AND** no unhandled error is raised

### Requirement: Reverse sync lag produces a logged event
The system SHALL observe when the JSONL source is significantly newer than the cold mirror (configured threshold) and SHALL emit a logged event so the operator can investigate broken cold-sync.

#### Scenario: Reverse lag is logged
- **WHEN** the JSONL updated-at exceeds the cold-mirror updated-at by more than the configured reverse-lag threshold
- **THEN** the system emits a logged event that names the session key and both timestamps

### Requirement: Admin dry-run lists stale candidates without mutation
The admin tool SHALL list every session that would be recovered in the configured mode and SHALL NOT mutate any file when invoked with `--dry-run`. The admin tool SHALL obtain its candidate list from the same staleness detector used by the runtime.

#### Scenario: Dry-run reports candidates only
- **WHEN** the operator runs `tools/recover_stale_sessions.py --dry-run`
- **THEN** the tool prints session keys, JSONL and PG timestamps, and the mode that would apply
- **AND** no JSONL or backup file is created, moved, or deleted
