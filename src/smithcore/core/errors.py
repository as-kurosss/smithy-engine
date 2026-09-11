"""Error types for tool execution and the agent framework."""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """Structured error for tool execution.

    Subclasses:
        InvalidInput — invalid or missing input parameters.
        ElementNotFound — UI element not found or inaccessible.
        Cancelled — operation cancelled by user or authority.
        PlatformError — platform or UIA error with underlying cause.
        BusinessError — transaction data is invalid, retry is pointless.
        InfrastructureError — infrastructure failure, retry may help.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidInput(ToolError):
    """Invalid or missing input parameters."""

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        input_value: Any = None,
    ) -> None:
        super().__init__(message)
        self.param = param
        self.input_value = input_value

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({str(self)!r}, "
            f"param={self.param!r}, input_value={self.input_value!r})"
        )


class ElementNotFound(ToolError):
    """UI element not found or inaccessible."""

    def __init__(
        self,
        message: str = "Element not found",
        *,
        selector: Any = None,
    ) -> None:
        super().__init__(message)
        self.selector = selector

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r}, selector={self.selector!r})"


class Cancelled(ToolError):
    """Operation cancelled by user or authority."""

    def __init__(self) -> None:
        super().__init__("Operation cancelled")


class PlatformError(ToolError):
    """Platform or UIA error with underlying cause."""

    def __init__(
        self,
        message: str,
        *,
        source: BaseException | None = None,
        input_value: Any = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.input_value = input_value


class BusinessError(ToolError):
    """Transaction data is invalid — retrying the same payload is pointless.

    Raised by ``process_fn`` inside the transaction runner to mark the
    item as ``business_failed`` (terminal, no requeue).
    """


class InfrastructureError(ToolError):
    """Infrastructure failure — retrying may help.

    Raised by ``process_fn`` (or produced by the runner from unexpected
    exceptions) to mark the item as ``system_failed`` (requeued until
    the queue's ``max_attempts`` budget is exhausted).
    """


class ConfigError(InvalidInput):
    """Robot config is missing, unreadable, or fails validation.

    Raised once with every problem listed — the robot must not start
    with a half-valid config.
    """
