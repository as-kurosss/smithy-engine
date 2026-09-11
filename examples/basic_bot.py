"""Basic RPA bot example — launch Notepad and interact with it.

Requires: pip install smithcore[windows]
"""

import asyncio

from smithcore import Smithcore
from smithcore.windows.tools.click import ClickTool
from smithcore.windows.tools.delay import DelayTool
from smithcore.windows.tools.process import ProcessTool
from smithcore.windows.tools.wait import WaitTool

bot = Smithcore(tools=[ProcessTool(), ClickTool(), WaitTool(), DelayTool()])


async def main() -> None:
    app = await bot.process_run("notepad.exe")

    await bot.wait(app, class_name="Notepad", name="*Notepad")
    await bot.click(app, name="File")
    await bot.delay(duration_ms=300)
    await bot.click(app, name="Save As...")

    print(f"Notepad launched with PID: {app.pid}")
    await bot.process_stop(app)


if __name__ == "__main__":
    asyncio.run(main())
