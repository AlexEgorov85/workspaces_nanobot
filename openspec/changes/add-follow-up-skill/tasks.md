## Phase A — Навык

- [x] A.1 `workspace/skills/follow_up/SKILL.md` — фронтматтер (`name`,
      `description`, `metadata.nanobot.always: true`), таблица «когда
      Follow Up / когда `audit_analyzer`», семь инструментов
      `mcp_follow_up_*`, контракт дословного вывода, порядок ожидания
      карточки (`card_start` → `card_status`, вызов сам ждёт до 20 с).
- [x] A.2 `workspace/skills/follow_up/scripts/follow_up_mcp` — лаунчер
      (stdlib, shebang, бит исполнения), `follow_up_mcp.cmd` — Windows.
- [x] A.3 `follow_up.env.local.example` — образец машинного файла.
- [x] A.4 `__init__.py` в папке навыка и `scripts/` — как у соседей.

## Phase B — Реестры

- [x] B.1 `config.json` → `tools.mcpServers.follow_up` (статичный блок,
      `tool_timeout: 120`).
- [x] B.2 `project.json` → `skills.follow_up: {"enabled": true}` с
      комментарием, почему без `tables`/`vector_indexes`.

## Phase C — Документация и тесты

- [x] C.1 `tests/test_follow_up_skill.py`: фронтматтер, набор имён
      инструментов, статичность блока, запись в реестре, stdlib-only,
      код 3 и чистый stdout без настройки, разбор `follow_up.env.local`.
- [x] C.2 CHANGELOG `[Unreleased] → Added`, README (одна строка),
      `docs/skill-tool-inventory.md` (одна строка).
- [x] C.3 Прогон `pytest tests/ -m "not live and not integration"` и
      `tests/contract/` — зелёные.

## Phase D — Приёмка (на стороне владельца)

- [ ] D.1 Слить ветку; на машине gateway заполнить
      `workspace/skills/follow_up/follow_up.env.local` (или запустить
      `scripts/install_into_nanobot.py` из репозитория Follow Up).
- [ ] D.2 Перезапустить gateway (`--profile=prod`), увидеть в логе
      `MCP server 'follow_up': connected, 7 capabilities registered`.
- [ ] D.3 Из чата Единого рабочего места (К1-2) спросить про акт проверки.
- [ ] D.4 Архивировать change, перенести спеку в
      `openspec/specs/architecture/external-process-skill/spec.md`.
