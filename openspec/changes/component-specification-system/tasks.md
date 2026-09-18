# Tasks: Component Specification System

## Задачи этого change

### 1. Создать модель компонента

- [x] Создать `openspec/specs/architecture/component-model/spec.md`
  - Определить обязательный шаблон компонентной спецификации
  - Зафиксировать русский язык как нормативный
  - Определить словарь терминов
  - Установить правила именования и размещения
  - Описать разделение ответственности документации

### 2. Создать реестр компонентов

- [x] Создать `openspec/specs/COMPONENTS.md`
  - Добавить таблицу всех известных компонентов
  - Указать статусы спецификаций
  - Добавить план работ по волнам

### 3. Подготовить инфраструктуру change

- [x] Создать `openspec/changes/component-specification-system/proposal.md`
- [x] Создать `openspec/changes/component-specification-system/design.md`
- [x] Создать `openspec/changes/component-specification-system/tasks.md` (этот файл)
- [ ] Создать `.openspec.yaml` для change

### 4. Обновить существующие спецификации (следующие изменения)

- [ ] `architecture/skill-tool-boundary`:
  - Перевести на русский язык
  - Добавить раздел Boundary
  - Добавить раздел Dependencies
  - Добавить раздел Implementation Reference
  - Привести к единому шаблону

- [ ] `configuration/profiles`:
  - Перевести на русский язык
  - Добавить раздел Boundary
  - Добавить раздел Dependencies
  - Добавить раздел Implementation Reference
  - Привести к единому шаблону

- [ ] `runtime/context`:
  - Перевести на русский язык
  - Добавить раздел Boundary
  - Добавить раздел Dependencies
  - Добавить раздел Implementation Reference
  - Привести к единому шаблону

- [ ] `data/cache` → `data/cache-provider`:
  - Переименовать директорию
  - Перевести на русский язык
  - Добавить раздел Boundary
  - Добавить раздел Dependencies
  - Добавить раздел Implementation Reference
  - Привести к единому шаблону

- [ ] `data/vector-indexes`:
  - Перевести на русский язык
  - Добавить раздел Boundary
  - Добавить раздел Dependencies
  - Добавить раздел Implementation Reference
  - Привести к единому шаблону

### 5. Обновить документацию проекта

- [ ] Обновить `docs/README.md`:
  - Добавить ссылку на `openspec/specs/COMPONENTS.md`
  - Обновить навигацию

- [ ] Обновить `AGENTS.md`:
  - Добавить правила работы со спецификациями
  - Указать обязательность сверки с компонентными спеками

### 6. Создать спецификации первой волны (отдельные changes)

- [ ] `runtime/application-context` — обновить существующую
- [ ] `runtime/agent-factory` — новая
- [ ] `runtime/message-bus` — новая
- [ ] `configuration/config-service` — новая
- [ ] `sessions/postgres-session-manager` — новая
- [ ] `channels/postgres-channel` — новая
- [ ] `channels/redis-channel` — новая
- [ ] `data/cache-provider` — обновить
- [ ] `data/vector-indexes` — обновить

## Критерии приёмки

### Для этого change

- [x] Модель компонента определена и задокументирована
- [x] Единый шаблон спецификации определён
- [x] Русский язык зафиксирован как нормативный
- [x] Реестр компонентов создан
- [x] Правила именования и размещения определены
- [x] Разделение ответственности задокументировано
- [ ] Change сам прошёл validate/apply/archive workflow

### Для последующих изменений

- [ ] Все существующие 5 OpenSpec приведены к новой модели
- [ ] Созданы спецификации первой волны для core/runtime компонентов
- [ ] Для каждой спецификации существует явная ссылка на реализацию
- [ ] Нет второго registry
- [ ] Нет дублирующих нормативных описаний
- [ ] `docs/README.md` обновлён
- [ ] `AGENTS.md` обновлён

## Зависимости

Этот change не зависит от других изменений кода. Это чисто документационное изменение, создающее инфраструктуру для будущих спецификаций.

## Риски

1. **Создание избыточных спецификаций** — риск создать спецификации для слишком мелких единиц кода.
   - Митигация: следовать правилам определения компонента из `component-model/spec.md`.

2. **Дублирование с TARGET_ARCHITECTURE.md** — риск перенести глобальные архитектурные правила в компонентные спецификации.
   - Митигация: явно разделять global architecture rules и component-specific contracts.

3. **Устаревание спецификаций** — риск, что спецификации не будут обновляться при изменении кода.
   - Митигация: будущая CI-проверка, обязательность обновления спецификаций при изменении контракта компонента.

4. **Английский язык в спецификациях** — риск смешения языков.
   - Митигация: явное правило в `component-model/spec.md`, будущая CI-проверка.

## Timeline

- **Wave 0 (этот change)**: Инфраструктура системы спецификаций
- **Wave 1**: Обновление существующих 5 спецификаций + создание spec для application-context
- **Wave 2**: Core runtime компоненты (agent-factory, message-bus, config-service)
- **Wave 3**: Channels, sessions, data layer
- **Wave 4**: Observability, infrastructure, security
- **Wave 5**: Interfaces, testing
- **Wave 6**: Skills

Каждая волна — отдельный OpenSpec change.
