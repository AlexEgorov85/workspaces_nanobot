## Phase A — Навык

- [x] A.1 `workspace/skills/follow_up/SKILL.md` — фронтматтер (`name`,
      `description`, `metadata.nanobot.always: true`), таблица «когда
      Follow Up / когда `audit_analyzer`», семь инструментов
      `mcp_follow_up_*`, контракт дословного вывода, порядок ожидания
      карточки (`card_start` → `card_status`, вызов сам ждёт до 20 с).
- [x] A.2 `workspace/skills/follow_up/backend/` — код сервера навыка;
      `tools.json` — его описания и схемы инструментов.
- [x] A.3 `scripts/follow_up_mcp` — лаунчер (stdlib, shebang, бит
      исполнения): код рядом → интерпретатор агента; `--where`, `--check`;
      `follow_up_mcp.cmd` — Windows.
- [x] A.4 `requirements.txt` навыка — без пакетов, закреплённых в корневом.
- [x] A.5 `ruff.toml` (исключает `backend/`, включает лаунчер),
      `.gitignore` (рабочие данные; каталоги кода — в git).
- [x] A.6 `follow_up.env.local.example` — только для отдельного клона.

## Phase B — Регистрация

- [x] B.1 `workspace/tools/follow_up.py` — семь инструментов агента,
      мост к серверу по MCP stdio; подъём заранее только в `gateway.py`.
- [x] B.2 `project.json` → `skills.follow_up: {"enabled": true}` с
      комментарием, почему без `tables`/`vector_indexes`.
- [x] B.3 `config.json` не меняется (`tools.mcpServers` в 0.3.5 gateway
      проекта не читает).

## Phase C — Документация и тесты

- [x] C.1 `tests/test_follow_up_skill.py`: фронтматтер, набор имён
      инструментов, нет записи в `tools.mcpServers`, stdlib-only лаунчер,
      код находится без настройки, код 3 и чистый stdout без кода;
      инструменты агента = `tools.json` = `SKILL.md`, настоящий загрузчик
      регистрирует семь, подъём только в gateway и один раз, мост на
      MCP-сервере-заглушке (вызов, окружение, перезапуск, зависший старт);
      изоляция импортов, `requirements.txt`, `ruff.toml`, `.gitignore`.
- [x] C.2 CHANGELOG `[Unreleased] → Added`, README (одна строка),
      `docs/skill-tool-inventory.md` (одна строка).
- [x] C.3 `pytest tests/ -m "not live and not integration"` и
      `ruff check lib workspace tests tools` — без новых падений
      относительно master.

## Phase D — Приёмка (на стороне владельца)

- [ ] D.1 Слить ветку, «Получить из master» на машине gateway.
- [ ] D.2 `python workspace/skills/follow_up/scripts/follow_up_mcp --check`
      — `ok: true`; иначе поставить `requirements.txt` навыка.
- [ ] D.3 Перезапустить gateway (`--profile=prod`), увидеть семь
      `mcp_follow_up_*` среди project tools и `follow_up: сервер навыка поднят`.
- [ ] D.4 Из чата Единого рабочего места (К1-2) спросить про акт проверки.
- [ ] D.5 Архивировать change, перенести спеку в
      `openspec/specs/architecture/external-process-skill/spec.md`.
