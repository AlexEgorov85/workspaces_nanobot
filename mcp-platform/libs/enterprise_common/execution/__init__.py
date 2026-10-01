"""Слой исполнения операций.

Что здесь и зачем по отдельности:

* `context` — идентичность вызова и неизменяемый снимок оборота. Идентичность
  приходит в `params._meta` и **не достраивается** никогда;
* `policy` — пороги и флаги вызова, разрешаемые «платформа → capability →
  операция». Значений по умолчанию, зашитых в код, нет: они живут в
  `platform.json`;
* `quality` — проверки результата, разделённые на технические (провал — отказ)
  и семантические (провал — признак);
* `errors` — приведение любого отказа к коду из закрытого списка;
* `logger` — сборка событий вызова без записи в базу;
* `pipeline` — девять шагов целиком.

Пакет не импортирует `registry`: `ToolDefinition` нужен конвейеру только для
аннотации, а рантаймовая зависимость дала бы цикл `registry → execution →
registry`.
"""

from .context import (
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    KEY_USER_ID,
    META_PREFIX,
    REQUIRED_KEYS,
    IdentityMissingError,
    McpCallContext,
    ToolExecutionContext,
    new_request_id,
)
from .errors import (
    FAILURE_CODES,
    RETRYABLE_CODES,
    ExecutionTimeout,
    Failure,
    failure_from_payload,
    normalize_exception,
)
from .logger import ExecutionLogger, collect_argument_fields, measure
from .pipeline import (
    EXECUTION_KEY,
    LEGACY_IDENTITY_KEYS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    PipelineResult,
    ToolExecutionPipeline,
)
from .policy import SETTING_NAMES, ExecutionPolicy, resolve_policy
from .quality import (
    POLICY_NAMES,
    QUALITY_POLICIES,
    CheckOutcome,
    QualityChecker,
    QualityReport,
    default_checker,
    technical_failure,
)

__all__ = [
    "EXECUTION_KEY",
    "FAILURE_CODES",
    "KEY_REQUEST_ID",
    "KEY_SESSION_ID",
    "KEY_USER_ID",
    "LEGACY_IDENTITY_KEYS",
    "META_PREFIX",
    "POLICY_NAMES",
    "QUALITY_POLICIES",
    "REQUIRED_KEYS",
    "RETRYABLE_CODES",
    "SETTING_NAMES",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "CheckOutcome",
    "ExecutionLogger",
    "ExecutionPolicy",
    "ExecutionTimeout",
    "Failure",
    "IdentityMissingError",
    "McpCallContext",
    "PipelineResult",
    "QualityChecker",
    "QualityReport",
    "ToolExecutionContext",
    "ToolExecutionPipeline",
    "collect_argument_fields",
    "default_checker",
    "failure_from_payload",
    "measure",
    "new_request_id",
    "normalize_exception",
    "resolve_policy",
    "technical_failure",
]
