# MCP Platform

Enterprise-слой, вынесенный из агента. **Nanobot тут не участвует.**

Эта папка — отдельный проект. Она не импортирует `nanobot`, не импортирует `lib/`,
не импортирует `workspace/`. Агент (Nanobot) общается с ней только по протоколу MCP.

## Жёсткие правила

Нарушение любого пункта = баг, даже если код работает.

1. **Nanobot неизменяем.** Не патчить установленный `nanobot-ai`, не форкать его,
   не добавлять в него enterprise-классы. Версия зафиксирована: `0.3.5`.
2. **Никаких импортов наверх.** В `mcp-platform/**` запрещено:
   - `import nanobot` / `from nanobot...`
   - `import lib` / `from lib...`
   - `import workspace` / `from workspace...`
3. **Никакого собственного AgentLoop.** Ни `AgentLoop`, ни `ToolContext`,
   ни `MessageBus`, ни LLM-обвязки Nanobot. MCP-сервер — это capability,
   а не второй агент.
4. **Один механизм за раз.** Не переносить несколько доменов в одной фазе.
5. **Тесты → перенос → интеграционный тест → удаление старого пути.**
   Нельзя удалять старую реализацию до прохождения интеграционных тестов MCP.
6. **Нельзя оставлять две рабочие реализации одного механизма.** После успешного
   переноса старый tool удаляется, а не остаётся «на всякий случай».
7. **Нет массовых рефакторингов «заодно».** Если пришлось импортировать
   `nanobot.*` — остановиться и переработать границу, а не подключить импорт.

Проверяется автоматически: `tests/test_architecture_boundaries.py`.

## Раскладка

```
mcp-platform/
├── pyproject.toml            # самостоятельный пакет, без nanobot-зависимости
├── requirements.txt          # только enterprise-стек, без nanobot
├── docs/
│   ├── BASELINE.md           # точка отсчёта миграции (фаза 0)
│   ├── MCP-CONTRACTS.md      # контракты операций: что делает, что возвращает, когда падает
│   ├── MIGRATION.md          # что куда переносится и в каком порядке
│   └── TARGET-ARCHITECTURE.md
├── libs/
│   ├── enterprise_common/    # config, models, errors, реестр инструментов
│   └── enterprise_data/      # postgres, vector (FAISS)
├── servers/
│   ├── _template/            # ЭТАЛОН. Копируется, а не выдумывается заново.
│   └── <name>/
│       ├── server.py         # bootstrap: поднять реестр, отдать MCP
│       ├── tools/*.py        # по файлу на инструмент
│       └── service.py
└── tests/
```

## Слои внутри одного сервера

```text
server.py   # тонкий bootstrap: собрать реестр и отдать его MCP
    ↓
tools/*.py  # по одному инструменту на файл: create_tool(container) → ToolDefinition
    ↓
service.py  # бизнес-логика домена. Ни MCP, ни Nanobot, ни SQL в промптах
    ↓
libs/enterprise_data   # доступ к данным
```

Сервер **не должен** содержать бизнес-логику и **не должен** знать список
инструментов заранее. Логика живёт в `service.py` и тестируется без MCP вообще.
Список инструментов приходит из `tools/` при старте.

## Как добавить сервер

1. Скопировать `servers/_template` в `servers/<name>`.
2. Реализовать `service.py`. Он обязан тестироваться без MCP.
3. Создать `tools/<operation>.py` для каждой операции (§ «Как добавить инструмент»).
4. Описать `server.py` как bootstrap реестра — без `@mcp.tool()` вручную.
5. Добавить `servers/<name>/tests/test_service.py` и тест загрузки реестра.
6. Проверить: `pytest` в `mcp-platform/` зелёный, `server.py` запускается
   без установленного nanobot.
7. Только после этого — подключать к агенту через `config.json::mcpServers`.

## Как добавить инструмент

Один файл. `server.py` не меняется.

```python
# servers/data/tools/history_search.py
def create_tool(container):
    def history_search(query: str, limit: int = 20) -> str:
        return container.data.history_search(query, limit)

    return ToolDefinition(
        name="history_search",
        description="Поиск по журналу agent_gateway_logs",
        handler=history_search,
        category="read",
        version="1.0",
    )
```

Порядок при старте: `tools/*.py` → `create_tool(container)` → валидация →
`registry.register()` → MCP. Валидатор проверяет импорт, наличие
`create_tool`, тип `ToolDefinition`, непустые `name`/`description`, уникальность
`name`, callable `handler` и валидность схемы аргументов.

**Ошибка любого одного файла останавливает старт целиком** — с именем файла и
`name`. Полузагруженный сервер не поднимается: иначе агент будет считать
capability существующей, а отказ всплывёт на реальном запросе пользователя.

**Hot reload нет.** Реестр иммутабелен в пределах процесса: добавил файл →
перезапустил сервер. Регистрация — это выполнение кода из каталога `tools/`,
поэтому каталог доверенный: туда попадает только код репозитория, и всё
исполняется с правами процесса.

## Подключение к агенту

Nanobot 0.3.5 читает `config.json → mcpServers` (`MCPServerConfig`):
`type` (`stdio`/`sse`/`streamableHttp`, авто-определение), `command`, `args`,
`env`, `cwd`, `url`, `headers`, `tool_timeout`, `enabled_tools`.

Локальные серверы — `stdio`. Имена инструментов приходят в агенте как
`mcp_<server>_<tool>`.

**Подключение — последний шаг переноса, не первый.** Сначала сервер работает
и покрыт тестами без агента.

## Локальный запуск

```bash
cd mcp-platform
pip install -r requirements.txt
python -m pytest -q          # юнит-тесты сервисов, без MCP и без агента
```

Проверка «стартует без Nanobot» — отдельным тестом в `tests/`.
