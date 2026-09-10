"""Smithy — Free Python RPA engine for creating automation bots."""

from smithy.core.config import Config, load_config
from smithy.core.errors import (
    BusinessError,
    Cancelled,
    ConfigError,
    ElementNotFound,
    InfrastructureError,
    InvalidInput,
    PlatformError,
    ToolError,
)
from smithy.core.http_queue import HttpQueue, HttpQueueError
from smithy.core.logging import JsonlEventLogger
from smithy.core.queue import (
    ClaimedItem,
    InMemoryQueue,
    LeaseRenewable,
    Queue,
    QueueInfo,
    QueueItem,
    SqliteQueue,
)
from smithy.core.retry import RetryTool
from smithy.core.schema import validate_against_schema
from smithy.core.selectors import SelectorStore
from smithy.core.tool import AbstractTool, Tool, tool
from smithy.core.transactions import (
    ItemOutcome,
    TransactionContextMiddleware,
    TransactionReport,
    current_transaction_id,
    run_transactions,
    run_transactions_async,
)
from smithy.facade import (
    ClickResult,
    InputTextResult,
    ProcessHandle,
    SetTextResult,
    Smithy,
)
from smithy.flow import FlowError, FlowRunner
from smithy.pack import (
    PACK_MANIFEST,
    TEMPLATE_FILE,
    build_pack,
    fetch_pack,
    load_manifest,
    load_template,
    publish_pack,
    validate_template,
    verify_pack,
    zip_pack,
)

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("smithy-engine")
except PackageNotFoundError:  # pragma: no cover — running from an uninstalled tree
    __version__ = "0.8.10"

__all__ = [
    "AbstractTool",
    "BusinessError",
    "Cancelled",
    "ClaimedItem",
    "ClickResult",
    "Config",
    "ConfigError",
    "ElementNotFound",
    "FlowError",
    "FlowRunner",
    "HttpQueue",
    "HttpQueueError",
    "InMemoryQueue",
    "InputTextResult",
    "InvalidInput",
    "ItemOutcome",
    "JsonlEventLogger",
    "LeaseRenewable",
    "PACK_MANIFEST",
    "PlatformError",
    "ProcessHandle",
    "Queue",
    "QueueInfo",
    "QueueItem",
    "RetryTool",
    "SelectorStore",
    "SetTextResult",
    "Smithy",
    "SqliteQueue",
    "InfrastructureError",
    "TEMPLATE_FILE",
    "Tool",
    "ToolError",
    "TransactionContextMiddleware",
    "TransactionReport",
    "build_pack",
    "current_transaction_id",
    "fetch_pack",
    "load_config",
    "load_manifest",
    "load_template",
    "publish_pack",
    "run_transactions",
    "run_transactions_async",
    "tool",
    "validate_against_schema",
    "validate_template",
    "verify_pack",
    "zip_pack",
]
