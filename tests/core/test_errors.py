"""Tests for smithcore.core.errors."""

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


class TestToolError:
    def test_tool_error_message(self) -> None:
        e = ToolError("bad input")
        assert str(e) == "bad input"
        assert isinstance(e, Exception)

    def test_tool_error_is_exception(self) -> None:
        assert issubclass(ToolError, Exception)


class TestInvalidInput:
    def test_basic_message(self) -> None:
        e = InvalidInput("missing param")
        assert str(e) == "missing param"
        assert e.param is None
        assert e.input_value is None

    def test_with_param_and_value(self) -> None:
        e = InvalidInput(
            "wrong type",
            param="timeout",
            input_value="abc",
        )
        assert e.param == "timeout"
        assert e.input_value == "abc"

    def test_inherits_tool_error(self) -> None:
        assert issubclass(InvalidInput, ToolError)


class TestElementNotFound:
    def test_default_message(self) -> None:
        e = ElementNotFound()
        assert str(e) == "Element not found"
        assert e.selector is None

    def test_custom_message_and_selector(self) -> None:
        e = ElementNotFound("no such button", selector={"name": "OK"})
        assert str(e) == "no such button"
        assert e.selector == {"name": "OK"}

    def test_inherits_tool_error(self) -> None:
        assert issubclass(ElementNotFound, ToolError)


class TestCancelled:
    def test_default_message(self) -> None:
        e = Cancelled()
        assert str(e) == "Operation cancelled"

    def test_inherits_tool_error(self) -> None:
        assert issubclass(Cancelled, ToolError)


class TestPlatformError:
    def test_basic(self) -> None:
        e = PlatformError("COM error")
        assert str(e) == "COM error"
        assert e.source is None
        assert e.input_value is None

    def test_with_source(self) -> None:
        original = ValueError("native error")
        e = PlatformError("COM failed", source=original)
        assert e.source is original

    def test_inherits_tool_error(self) -> None:
        assert issubclass(PlatformError, ToolError)


class TestErrorHierarchy:
    """Verify the full error hierarchy is consistent (single family)."""

    def test_tool_error_hierarchy(self) -> None:
        assert issubclass(InvalidInput, ToolError)
        assert issubclass(ElementNotFound, ToolError)
        assert issubclass(Cancelled, ToolError)
        assert issubclass(PlatformError, ToolError)
        assert issubclass(BusinessError, ToolError)
        assert issubclass(InfrastructureError, ToolError)
        assert issubclass(ConfigError, InvalidInput)
