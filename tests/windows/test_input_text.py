"""Tests for smithcore.windows.tools.input_text — plain text only."""

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.windows.tools.input_text import InputTextTool, _send


class TestInputText:
    def test_send_exists(self) -> None:
        # _send should be importable and callable
        assert callable(_send)

    @pytest.mark.asyncio
    async def test_missing_text_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="text"):
            await InputTextTool().execute({})
