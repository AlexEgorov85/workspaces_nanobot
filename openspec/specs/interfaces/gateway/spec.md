# Gateway Interface

## Назначение

Gateway Interface обеспечивает HTTP/REST API для внешнего взаимодействия с системой, предоставляя интерфейс для интеграции с внешними сервисами, веб-клиентами и мобильными приложениями.

## Ответственность

- Приём HTTP-запросов от клиентов
- Маршрутизация запросов к обработчикам
- Аутентификация и авторизация запросов
- Сериализация/десериализация JSON
- Управление сессиями через HTTP
- WebSocket поддержка для real-time коммуникации
- Rate limiting и защита от DoS

## Граница

### Владеет
- HTTP-сервером и роутингом
- Middleware (auth, logging, CORS)
- Форматом API responses
- Управлением WebSocket соединениями

### Не владеет
- Бизнес-логикой обработки запросов
- Хранением состояния сессий (делегирует SessionManager)
- Аутентификацией пользователей (проверяет токены)
- Генерацией HTML (это ответственность Streamlit)

## Контракт

### Публичный интерфейс

```python
class GatewayInterface:
    def start(self, host: str, port: int) -> None
    def stop(self) -> None
    def register_route(
        self,
        method: str,
        path: str,
        handler: Callable[[Request], Response]
    ) -> None
    
    def register_websocket(
        self,
        path: str,
        handler: WebSocketHandler
    ) -> None
    
    def is_running(self) -> bool
    def get_stats(self) -> GatewayStats
```

### Требования

#### Требование: Обработка HTTP-запросов

**Сценарий: Приём REST API запроса**
- **КОГДА** поступает HTTP-запрос на известный endpoint
- **ТОГДА** запрос маршрутизируется к соответствующему handler
- **И** выполняется аутентификация (если требуется)
- **И** вызывается handler с распарсенным request body
- **И** возвращается HTTP response с appropriate status code

#### Требование: WebSocket поддержка

**Сценарий: Real-time коммуникация**
- **КОГДА** клиент подключается по WebSocket
- **ТОГДА** устанавливается persistent соединение
- **И** сообщения передаются双向 между клиентом и системой
- **И** соединение закрывается при завершении сессии

#### Требование: Аутентификация

**Сценарий: Проверка доступа к API**
- **КОГДА** запрос требует аутентификации
- **ТОГДА** проверяется наличие и валидность токена
- **И** при отсутствии/невалидности возвращается 401 Unauthorized
- **И** при успехе запрос передаётся handler с контекстом пользователя

#### Требование: Rate Limiting

**Сценарий: Защита от злоупотреблений**
- **КОГДА** клиент превышает лимит запросов
- **ТОГДА** возвращается 429 Too Many Requests
- **И** лимиты настраиваются через конфигурацию
- **И** разные endpoints могут иметь разные лимиты

### Запрещённое поведение

Gateway Interface НЕ ДОЛЖЕН:
- Раскрывать внутренние ошибки в ответах (только message)
- Пропускать неаутентифицированные запросы к защищённым endpoints
- Блокироваться на обработку одного запроса бесконечно
- Хранить чувствительные данные в логах
- Позволять CORS для всех доменов без конфигурации

## Зависимости

### Может зависеть от
- ApplicationContext для доступа к сервисам
- SessionManager для управления HTTP-сессиями
- ConfigService для конфигурации сервера
- Logger для логирования запросов

### Не должен зависеть от
- Конкретных бизнес-компонентов
- Frontend-фреймворков
- Баз данных напрямую

## Конфигурация

```yaml
gateway:
  enabled: true
  host: "0.0.0.0"
  port: 8000
  workers: 4
  max_request_size_mb: 10
  timeout_sec: 30.0
  cors:
    enabled: true
    allowed_origins:
      - "https://app.example.com"
    allowed_methods:
      - GET
      - POST
      - PUT
      - DELETE
  auth:
    enabled: true
    token_header: "Authorization"
    token_prefix: "Bearer"
  rate_limiting:
    enabled: true
    requests_per_minute: 60
    burst_size: 10
  websocket:
    enabled: true
    ping_interval_sec: 30.0
    max_message_size_mb: 1
  logging:
    log_requests: true
    log_responses: false
    exclude_paths:
      - /health
      - /metrics
```

## Жизненный цикл

1. **Инициализация**: Загрузка конфигурации, регистрация routes
2. **Запуск сервера**: Начало прослушивания порта
3. **Обработка запросов**: Роутинг, аутентификация, выполнение handlers
4. **WebSocket сессии**: Управление persistent соединениями
5. **Завершение**: Graceful shutdown, закрытие соединений

## Состояние

```python
{
    "server": Optional[HTTPServer],
    "routes": Dict[str, RouteHandler],
    "websocket_connections": Dict[str, WebSocketConnection],
    "active_requests": int,
    "total_requests": int,
    "is_running": bool
}
```

## Инварианты

- Каждый route имеет уникальный path + method combination
- Все запросы логируются (если включено логирование)
- Timeout применяется ко всем запросам
- WebSocket соединения периодически пингуются
- При shutdown новые запросы отклоняются

## Поведение при ошибке

- 404 Not Found → неизвестный endpoint
- 401 Unauthorized → отсутствие/невалидность токена
- 403 Forbidden → недостаточно прав
- 429 Too Many Requests → превышен rate limit
- 500 Internal Error → непредвиденная ошибка (без деталей)
- Timeout → 504 Gateway Timeout

## Потребители

- Web Clients — браузерные приложения
- Mobile Apps — мобильные клиенты
- External Services — интеграции через API
- CLI Tools — альтернативный интерфейс к системе

## Реализация

Основная реализация:
- `lib/interfaces/gateway.py:GatewayInterface`

Связанные компоненты:
- `lib/core/application_context.py:ApplicationContext`
- `lib/sessions/postgres_session_manager.py:PGSessionManager`
- `lib/services/config_service.py:ConfigService`

## Проверка

### Автоматическая проверка
- Тесты на HTTP endpoints
- Тесты на аутентификацию
- Тесты на rate limiting
- Тесты на WebSocket соединения
- Нагрузочные тесты

### Ручная проверка
- Проверка API документации (OpenAPI/Swagger)
- Тестирование интеграций
- Анализ производительности
