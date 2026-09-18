# Реестр компонентов (Component Registry)

Единый реестр архитектурных компонентов проекта workspaces_nanobot.

Правила ведения реестра, статусы и формат записей зафиксированы в
[`openspec/specs/architecture/component-model/spec.md`](architecture/component-model/spec.md)
и [`openspec/specs/documentation/component-registry/spec.md`](documentation/component-registry/spec.md).

## Статистика

| Категория | Всего | Complete | Partial | Draft | Missing |
|-----------|-------|----------|---------|-------|---------|
| architecture | 2 | 0 | 2 | 0 | 0 |
| runtime | 1 | 0 | 1 | 0 | 0 |
| configuration | 1 | 0 | 1 | 0 | 0 |
| data | 2 | 0 | 2 | 0 | 0 |
| documentation | 1 | 0 | 0 | 1 | 0 |
| validation | 1 | 0 | 0 | 1 | 0 |
| **Итого** | **8** | **0** | **6** | **2** | **0** |

## Компоненты

### Architecture

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentModel | N/A (мета-спецификация) | [`architecture/component-model`](architecture/component-model/spec.md) | partial |
| SkillToolBoundary | N/A (архитектурное правило) | [`architecture/skill-tool-boundary`](architecture/skill-tool-boundary/spec.md) | partial |

### Runtime

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ApplicationContext | `lib/core/application_context.py:ApplicationContext` | [`runtime/context`](runtime/context/spec.md) | partial |

### Configuration

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| Profiles | `project.json::profiles` (конфигурация) | [`configuration/profiles`](configuration/profiles/spec.md) | partial |

### Data

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| CacheProvider | `lib/services/cache_provider.py:CacheProvider` | [`data/cache-provider`](data/cache-provider/spec.md) | partial |
| VectorIndexService | `lib/services/vector_index_service.py:VectorIndexService` | [`data/vector-indexes`](data/vector-indexes/spec.md) | partial |

### Documentation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentRegistry | N/A (мета-спецификация) | [`documentation/component-registry`](documentation/component-registry/spec.md) | draft |

### Validation

| Компонент | Реализация | Спецификация | Статус |
|-----------|------------|--------------|--------|
| ComponentSpecValidation | N/A (правила валидации) | [`validation/component-spec-validation`](validation/component-spec-validation/spec.md) | draft |

## План заполнения

### Wave 1 (завершён): Инфраструктура + миграция существующих spec

- [x] component-model
- [x] component-registry
- [x] component-spec-validation
- [x] skill-tool-boundary (миграция на русский + новый шаблон)
- [x] profiles (миграция на русский + новый шаблон)
- [x] cache-provider (миграция из data/cache + новый шаблон)
- [x] vector-indexes (новый шаблон)
- [x] context (новый шаблон)
