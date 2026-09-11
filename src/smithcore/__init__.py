"""Smithcore — Free Python RPA engine for creating automation bots."""

from smithcore.core.config import Config, load_config
from smithcore.core.errors import (
    BusinessError,
    Cancelled,
    ConfigError,
    ElementNotFound,
    InfrastructureError,
    InvalidInput,
    PlatformError,
    ToolError,
)
from smithcore.core.http_queue import HttpQueue, HttpQueueError
from smithcore.core.logging import JsonlEventLogger
from smithcore.core.queue import (
    ClaimedItem,
    InMemoryQueue,
    LeaseRenewable,
    Queue,
    QueueInfo,
    QueueItem,
    SqliteQueue,
)
from smithcore.core.retry import RetryTool
from smithcore.core.schema import validate_against_schema
from smithcore.core.selectors import SelectorStore
from smithcore.core.tool import AbstractTool, Tool, tool
from smithcore.core.transactions import (
    ItemOutcome,
    TransactionContextMiddleware,
    TransactionReport,
    current_transaction_id,
    run_transactions,
    run_transactions_async,
)
from smithcore.facade import (
    ClickResult,
    InputTextResult,
    ProcessHandle,
    SetTextResult,
    Smithcore,
)
from smithcore.flow import FlowError, FlowRunner
from smithcore.pack import (
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

    __version__ = _pkg_version("smithcore-engine")
except PackageNotFoundError:  # pragma: no cover — running from an uninstalled tree
    __version__ = "0.8.11"

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
    "Smithcore",
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
