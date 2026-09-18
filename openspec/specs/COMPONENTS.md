# Реестр компонентов (Component Registry)

Единый реестр архитектурных компонентов проекта workspaces_nanobot.

## Статистика

| Категория | Всего | Complete | Partial | Draft | Missing |
|-----------|-------|----------|---------|-------|---------|
| architecture | 2 | 0 | 1 | 0 | 1 |
| runtime | 1 | 0 | 1 | 0 | 0 |
| configuration | 1 | 0 | 1 | 0 | 0 |
| data | 2 | 0 | 1 | 0 | 1 |
| documentation | 1 | 0 | 0 | 1 | 0 |
| validation | 1 | 0 | 0 | 1 | 0 |
| **Итого** | **8** | **0** | **4** | **2** | **2** |

## Компоненты

### Architecture

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentModel | N/A (мета-спецификация) | `architecture/component-model` | partial |
| SkillToolBoundary | N/A (архитектурное правило) | `architecture/skill-tool-boundary` | missing |

### Runtime

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ApplicationContext | `lib/core/application_context.py:ApplicationContext` | `runtime/context` | partial |

### Configuration

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| Profiles | `project.json::profiles` (конфигурация) | `configuration/profiles` | partial |

### Data

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| CacheProvider | `lib/services/cache_provider.py:CacheProvider` | `data/cache-provider` | partial |
| VectorIndexService | `lib/data/vector_index_service.py:VectorIndexService` | `data/vector-indexes` | partial |

### Documentation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentRegistry | N/A (мета-спецификация) | `documentation/component-registry` | draft |

### Validation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentSpecValidation | N/A (правила валидации) | `validation/component-spec-validation` | draft |

## Правила ведения реестра

1. **Каждый production-компонент зарегистрирован**: компонент, существующий в коде, должен иметь запись в реестре.

2. **Честные статусы**:
   - `missing` — spec отсутствует
   - `draft` — spec создана, требует проверки
   - `partial` — spec заполнена частично
   - `complete` — spec прошла проверку полноты
   - `deprecated` — компонент устарел

3. **Реестр не является вторым источником истины**: содержит только метаданные, не описание поведения.

4. **Нет дубликатов**: каждый компонент имеет одну запись.

## План заполнения

### Wave 1 (текущий change): Инфраструктура + миграция существующих spec

- [x] component-model
- [x] component-registry
- [x] component-spec-validation
- [ ] skill-tool-boundary (миграция)
- [ ] profiles (миграция)
- [ ] cache-provider (миграция из data/cache)
- [ ] vector-indexes (миграция)
- [ ] context (миграция)
