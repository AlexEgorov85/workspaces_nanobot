# logging-db Specification

## REMOVED Requirements

### Requirement: Skill invocation is out of scope

**Reason**: Требование снимается и заменяется: прежняя редакция описывала вызов Skill через
`tools.exec` и требовала, чтобы он порождал `tool_call`. Новое требование запрещает
`tools.exec` как несущий путь вызова Skill и ссылается на то, что инструмент
отключён — `tools.exec.enable = false` в `config.json`. Старый сценарий описывает
путь, который не может сработать, а его утверждение прямо отрицает главный вывод
нового требования, поэтому удержать его в новой редакции было бы невозможно.

`MODIFIED` здесь структурно неприменим: правило «MODIFIED не выбрасывает
сценарий» не различает «сценарий забыли скопировать» и «сценарий отменён новым
требованием», а здесь сценарий отменён. Поэтому старая редакция объявлена снятой,
а не переписана.
## ADDED Requirements

### Requirement: Skill не вызывается через tools.exec

The system SHALL NOT иметь dedicated runtime
event_type для «активации Skill». Skill в
текущей архитектуре — это content (markdown-инструкции
в `SKILL.md`), который загружается в agent context
через `SkillsLoader.load_skills_for_context(...)` /
`build_skills_summary(...)` (`nanobot/agent/skills.py`),
а не runtime-callable сущность.

Границы контракта:

- **Загрузка / обнаружение / инжекция `SKILL.md`
  в system prompt** НЕ порождает `skill_call` (или
  любой другой) structured event в
  `agent_gateway_logs`. Это внутреннее
  техническое состояние runtime.
- **Вызов функциональности Skill инструментом агента**
  логируется как штатная пара `event_type="tool_call"`
  (`name="<имя инструмента>"`, payload содержит `args`) +
  `event_type="tool_result"` (payload содержит
  `result`). Это **не отдельный `skill_call`** —
  это `tool_call`/`tool_result` с характерным
  payload. Правило не зависит от того, какой это
  инструмент: проектный (`audit_analyzer_query`,
  `legal_summarizer_query`) или иной.
- **`tools.exec` SHALL NOT быть несущим путём вызова
  Skill.** Инструмент отключён
  (`tools.exec.enable = false`), поэтому пример в
  требовании обязан называть живой инструмент:
  иначе сценарий непроверяем и требование
  описывает несуществующий путь.
- **Если** в будущем nanobot или этот проект
  введёт runtime API вида
  `SkillExecutor.invoke(skill_name, ...)` —
  это отдельное архитектурное изменение,
  которое вводит соответствующий event_type
  через отдельный OpenSpec change. Эта
  спецификация не предвосхищает этот контракт.

Запрещено в runtime-коде:

- Эмитить `event_type="skill_call"` (или
  `skill_invocation`, `skill_activation`,
  любой аналогичный) — нет runtime-call
  site, нет соответствующего contract.
- Вводить `DbLoggingService.log_skill_call(...)` —
  единственный допустимый путь логирования
  Skill-вызовов уже покрыт существующими
  `log_tool_call` / `log_tool_result` (потому
  что вызов Skill идёт через tool).
- Эмитить events при `SkillsLoader.list_skills(...)`,
  `load_skill(...)`, `load_skills_for_context(...)`,
  `build_skills_summary(...)` или аналогичных
  чисто-loader методах.

#### Scenario: Вызов Skill через инструмент порождает tool_call, не skill_call

- **КОГДА** агент вызывает функциональность Skill
  через инструмент (например `audit_analyzer_query`;
  ранее это был `tools.exec("python
  skills/audit_analyzer/scripts/cli.py ...")`)
- **ТОГДА** `DbLoggingService` SHALL получить
  `LogEvent` с `event_type="tool_call"`,
  `name="<имя этого инструмента>"`, `actor="agent"`,
  payload содержит `args`
- **И** `DbLoggingService` SHALL получить
  соответствующий `LogEvent` с
  `event_type="tool_result"`,
  `name="<имя этого инструмента>"`, payload
  содержит `result`
- **И** `skill_call` (или любой другой
  skill-typed event) SHALL NOT быть эмитирован

#### Scenario: Загрузка SKILL.md в context не порождает event

- **КОГДА** `SkillsLoader.load_skills_for_context(...)`
  или `build_skills_summary(...)` выполняется
  при построении agent context
- **ТОГДА** `DbLoggingService` SHALL NOT получить
  никакой `LogEvent` (никакой `skill_call`,
  `skill_loaded`, `skill_discovered`, и т.п.)
- **И** `agent_gateway_logs` SHALL NOT содержать
  записей, привязанных к этому вызову loader'а
### Requirement: Один факт — одна строка

Один вызов tool'а MUST порождать ровно одну строку начала и
ровно одну строку завершения — независимо от того,
исполнялся он в процессе платформы или в процессе агента.

Сегодня это не так: агент пишет `tool_call` + `tool_result`,
платформа пишет `tool.started` + `tool.completed` +
`tool.failed`, и на MCP-вызовах это **4 строки на один
вызов**. Два писателя описывают один факт, и по таблице
невозможно ни посчитать число вызовов, ни отличить
платформенный вызов от агентского.

Правило владения: факт вызова пишет **тот, кто его
исполнял**. Операции, исполняемые конвейером платформы,
пишет платформа (`tool.*`); tool'ы, исполняемые в процессе
агента, пишет хук агента. В обоих случаях — одна строка
начала и одна завершения, а не сумма двух писателей.

Требование **не отменяет** ограничения «Skill invocation is
out of scope»: вызов функциональности Skill инструментом
агента по-прежнему логируется как вызов tool'а, а не
отдельным `skill_call`.

**Проверка:** замер на живых данных после правки —
`SELECT count(*) FROM agent_gateway_logs WHERE event_type =
'tool.started'` должен совпадать с числом фактических
вызовов (сверяется с `written_by_type` хука за то же
окно); тест на взаимную исключительность: ни одна пара
идентификаторов оборота и вызова не SHALL иметь одновременно
агентскую и платформенную строку начала.

#### Scenario: MCP-вызов описан одной парой строк

- **КОГДА** агент вызывает операцию через `enterprise-mcp`
- **ТОГДА** в журнале SHALL быть ровно одна `tool.started` и
  ровно одна `tool.completed` (или `tool.failed`) с одним и
  тем же `tool_call_id`
- **И** агентские `tool_call` / `tool_result` для этого
  вызова SHALL NOT быть записаны
