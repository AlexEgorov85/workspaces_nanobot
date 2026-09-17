## Purpose

Описывает контракт read-only follow-up запросов к skill `legal_summarizer`
через tool `legal_summarizer_query`: IPC-протокол между wrapper и
`workspace/skills/legal_summarizer/scripts/cli_query.py`, структуру
диагностики manifest и поведение wrapper при success / domain error /
process failure.

## ADDED Requirements

### Requirement: Subprocess IPC contract between tool wrapper and CLI query
The system SHALL define an IPC contract between the `legal_summarizer_query`
tool wrapper and `cli_query.py` that distinguishes successful responses,
domain errors, and process failures.

#### Scenario: Successful response
- **WHEN** `cli_query.py` exits with `returncode = 0` AND writes a
  parseable JSON object with `status = "ok"` to stdout
- **THEN** the wrapper SHALL return that JSON object as the tool result
  with `status = "ok"` preserved

#### Scenario: Domain error with structured JSON
- **WHEN** `cli_query.py` exits with `returncode != 0` AND writes a
  parseable JSON object with `status = "error"` to stdout
- **THEN** the wrapper SHALL return that JSON object as the tool result,
  preserving `error_type`, `message`, and all structured fields
- **AND** the wrapper SHALL NOT add its own generic error envelope on top

#### Scenario: Process failure with invalid stdout
- **WHEN** `cli_query.py` exits with `returncode != 0` AND stdout is empty
  OR contains text that is not parseable as JSON
- **THEN** the wrapper SHALL return its own error envelope with
  `status = "error"` and `error_type = "cli_failed"`
- **AND** the error message SHALL include the non-zero `returncode` and
  the first fragment of `stderr` for diagnostics

#### Scenario: Empty response on success exit
- **WHEN** `cli_query.py` exits with `returncode = 0` AND stdout is empty
- **THEN** the wrapper SHALL return its own error envelope with
  `status = "error"` and `error_type = "empty_response"`

### Requirement: Manifest diagnostic taxonomy
The system SHALL distinguish three reasons a manifest is unavailable for
follow-up queries: missing, corrupted, and unsupported version.

#### Scenario: Manifest file missing
- **WHEN** `cli_query.py` is invoked for an `operation_id` whose manifest
  file does not exist on disk
- **THEN** the wrapper SHALL receive an error envelope with
  `error_type = "manifest_not_found"` and `status = "error"`
- **AND** the error envelope SHALL include `operation_id` and a path hint
  showing where the manifest was expected

#### Scenario: Manifest JSON corrupted
- **WHEN** `cli_query.py` is invoked for an `operation_id` whose manifest
  file exists but cannot be parsed as valid JSON
- **THEN** the wrapper SHALL receive an error envelope with
  `error_type = "manifest_corrupted"` and `status = "error"`

#### Scenario: Manifest version unsupported
- **WHEN** `cli_query.py` is invoked for an `operation_id` whose manifest
  parses as JSON but its `version` field is missing or not equal to the
  current manifest version
- **THEN** the wrapper SHALL receive an error envelope with
  `error_type = "manifest_unsupported_version"` and `status = "error"`
- **AND** the error envelope SHALL include the observed `version` value if
  present

### Requirement: Backward compatibility of resume-path manifest loading
The system SHALL keep the existing `load_manifest()` function in
`workspace/skills/legal_summarizer/scripts/cache/manifest.py` returning
`None` for any of the three unavailable cases (missing / corrupted /
unsupported version) without leaking diagnostics through its return value.

#### Scenario: Resume loader unchanged
- **WHEN** the resume pipeline of `legal_summarizer` reads a manifest via
  `load_manifest()`
- **THEN** the function SHALL continue to return `None` on missing,
  corrupted, or unsupported-version manifest, exactly as before
- **AND** the new diagnostic capability SHALL be exposed through a
  separate function (e.g. `diagnose_manifest`) used only by
  `cli_query.py`

### Requirement: Documented semantics for chunks_total vs field=chunks
The system SHALL document that `chunks_total` from the manifest and
`field=chunks` from the query tool describe different things: the former
is a logical/planned chunk count, the latter is a list of physical
partial-result files under `operation/chunks/*.json`.

#### Scenario: Divergence between chunks_total and chunks list is not a bug
- **WHEN** `cli_query.py --field stats` returns `chunks_total = N` AND
  `cli_query.py --field chunks` returns a list of length `M` where
  `N != M`
- **THEN** the system SHALL treat this as a valid state, not an error
- **AND** `workspace/skills/legal_summarizer/SKILL.md` SHALL state
  explicitly that the two are independent sources

### Requirement: Wrapper preserves existing non-process error types
The system SHALL keep the wrapper's pre-existing error types
(`timeout`, `cli_not_found`, `subprocess_error`, `empty_response`,
`invalid_json`) wired to the same code paths after the IPC change.

#### Scenario: Timeout on subprocess
- **WHEN** `cli_query.py` exceeds `timeout_sec` from
  `tools.legal_summarizer_query.timeout_sec`
- **THEN** the wrapper SHALL return `status = "error"` with
  `error_type = "timeout"` and the same stderr-fragment policy as before

#### Scenario: CLI binary missing
- **WHEN** `cli_query.py` is not found at the resolved absolute path
- **THEN** the wrapper SHALL return `status = "error"` with
  `error_type = "cli_not_found"`

#### Scenario: Subprocess cannot start
- **WHEN** `subprocess.run` raises `OSError` before the child process
  starts
- **THEN** the wrapper SHALL return `status = "error"` with
  `error_type = "subprocess_error"`

### Requirement: No change to success-path schema
The system SHALL keep the existing `field` enum, `max_chunk_summary_chars`
range (`minimum = 100`, `maximum = 10000`, `default = 1500`), and the
shape of success responses for all fields unchanged.

#### Scenario: field=stats success
- **WHEN** `cli_query.py --field stats` succeeds
- **THEN** the tool result SHALL contain `status = "ok"`, `field = "stats"`,
  and the same metric keys as the current `_field_stats` output

#### Scenario: field=chunks success
- **WHEN** `cli_query.py --field chunks` succeeds
- **THEN** the tool result SHALL contain `status = "ok"`, `field = "chunks"`,
  `chunk_count`, and a `chunks` array with `chunk_id`, `section_id`,
  `section_path`, `page_start`, `page_end`, `summary` fields
