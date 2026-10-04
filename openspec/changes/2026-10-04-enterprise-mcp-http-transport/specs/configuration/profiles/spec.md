# configuration/profiles Specification — дельта

## Purpose

Оверлей профиля — закрытый список ключей, и порт платформы не имеет права в него
попасть. Дельта фиксирует запрет и объясняет, почему он не обсуждается: порт
различается между двумя одновременно живыми процессами, а не между двумя
контурами данных, и оверлей профиля решает ровно второе.

## Scope

`shared` — объявляет агент (`config.json`, `profiles/<mode>.jsonc`,
`config.py`), применяет валидатор оверлея (`config.py::validate_profile_overlay`).

Не в объёме: кто назначает порт и как агент узнаёт фактический
(`runtime/entrypoints`), способ доставки значения (`runtime/platform-settings`).

## ADDED Requirements

### Requirement: Порт платформы не ключ профиля

`gateway.agent.enterprise_mcp.port` MUST NOT становиться profile-owned ключом и
MUST NOT появляться в `profiles/<mode>.jsonc`.

Оверлей профиля — закрытый список из пяти ключей, и все пять имена таблиц
(`config.py::PROFILE_OWNED_RUNTIME_KEYS`). Любой посторонний ключ отвергается с
`ConfigurationError` на старте (`config.py::validate_profile_overlay`), а для
`prod` оверлей вообще не применяется: prod — чистый `config.json`
(`config.py::_merge_profile_overlay`). Поэтому адрес, объявленный по контурам,
был бы либо отвергнут валидатором, либо не прочитан вовсе.

Причина запрета содержательная. Ключ профиля означает «это значение различается
между контурами, потому что контур выбирает другой оверлей». Порт различается по
другой причине: он должен быть уникален между **одновременно живыми процессами**,
а не между контурами. Один контур — два процесса (gateway и CLI) — и два процесса
одного контура обязаны иметь разные порты так же строго, как два контура.
Прецедент в коде ровно об этом: `gateway.cache.local_path` помечен как
shared-ресурс с единым путём для gateway и CLI независимо от профиля, и
per-profile override ловится тем же reject «extra keys»
(`config.py:519-523`).

Уникальность порта обеспечивается не объявлением, а назначением: агент
запрашивает свободный порт, ОС выдаёт его, фактическое значение сообщает
платформа (`runtime/entrypoints`). Поэтому в оверлее порту нечего объявлять.

#### Scenario: Ключ порта в оверлее отвергается

- **WHEN** в `profiles/test.jsonc` добавлен ключ `gateway.agent.enterprise_mcp.port`
- **THEN** оверлей MUST быть отвергнут с `ConfigurationError`, называющим лишний
  ключ (проверяется
  `tests/test_profile_integration.py::test_validate_profile_overlay_rejects_extra_keys`)

#### Scenario: Для prod оверлея нет и не требуется

- **WHEN** система стартует в контуре `prod`
- **THEN** `profiles/prod.jsonc` MUST NOT требоваться и MUST NOT читаться, а
  запрос порта SHALL приходить из `config.json` (проверяется
  `tests/test_profile_lifecycle.py::test_gateway_prod_smoke_selects_prod_tables`)

#### Scenario: Два процесса одного контура не делят порт

- **WHEN** работают gateway и CLI одного контура одновременно
- **THEN** их процессы MUST иметь разные порты **независимо от профиля**
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPortUniqueness::test_same_profile_processes_get_distinct_ports`,
  `::test_no_port_in_profile_overlay`)
