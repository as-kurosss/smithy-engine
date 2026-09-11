"""REFramework-style transactional bot — dispatcher + performer skeleton.

Init (dispatcher) seeds the queue once, then the framework loop takes over:
claim → process_one → set_status → report. You only write process_one.

Requires: pip install smithcore[windows]

Queue backend is picked by environment, so local debugging can hit the
real orchestrator queue (handy for post-mortem debugging after a failed run):

    SMITHCORE_QUEUE=sqlite  local file bot-data/queue.db (default)
    SMITHCORE_QUEUE=cloud   orchestrator queue (needs SMITHCORE_API_URL,
                         SMITHCORE_AGENT_ID and SMITHCORE_TOKEN)

Stop cooperatively like UiPath's Stop button: create a file named STOP
next to this script. The current item finishes, new ones are not claimed.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from smithcore import (
    BusinessError,
    ClaimedItem,
    HttpQueue,
    Queue,
    Smithcore,
    SqliteQueue,
    TransactionContextMiddleware,
    load_config,
    run_transactions_async,
)
from smithcore.windows.tools.click import ClickTool
from smithcore.windows.tools.process import ProcessTool
from smithcore.windows.tools.wait import WaitTool

HERE = Path(__file__).resolve().parent

# Global config, loaded once in Init: frozen afterwards, attribute access.
# Missing keys fail here — never mid-run.
CONFIG = load_config(
    HERE / "reframework_bot.toml",
    required=["robot.queue", "robot.run_id", "retry.max_attempts"],
)

bot = Smithcore(tools=[ProcessTool(), ClickTool(), WaitTool()])
bot.add_middleware(TransactionContextMiddleware())  # stamp transaction_id into events


async def log_event(event: Any) -> Any:
    tx = event.metadata.get("transaction_id", "-")
    print(f"[{tx}] {event.tool_name} {event.duration_ms:.0f}ms")
    return event


bot.add_middleware(log_event)


def make_queue() -> Queue:
    """Local SQLite file by default; orchestrator queue when asked."""
    if os.getenv("SMITHCORE_QUEUE", "sqlite").lower() == "cloud":
        return HttpQueue(
            os.environ["SMITHCORE_API_URL"],
            agent_id=os.environ["SMITHCORE_AGENT_ID"],
            token=os.environ["SMITHCORE_TOKEN"],
        )
    db = HERE / "bot-data" / "queue.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return SqliteQueue(db)


def dispatcher(queue: Queue) -> None:
    """Init: read tasks from CSV and seed the queue (once per run)."""
    queue.get_or_create_queue(CONFIG.robot.queue, max_attempts=CONFIG.retry.max_attempts)
    rows = (HERE / "reframework_invoices.csv").read_text(encoding="utf-8").splitlines()
    for row in rows[1:]:  # skip the header
        invoice_id, amount = row.split(";")
        queue.add(CONFIG.robot.queue, {"invoice_id": invoice_id, "amount": float(amount)})


async def process_one(item: ClaimedItem) -> dict[str, Any] | None:
    """Process: handle ONE transaction. The loop around it is framework-owned."""
    payload = item.payload
    if payload["amount"] <= 0:
        # Bad data — no point retrying, goes straight to business_failed.
        raise BusinessError(f"Сумма не может быть {payload['amount']}")

    # Anything unexpected below (e.g. element not found) bubbles up and the
    # item is requeued as system_failed until max_attempts runs out.
    app = await bot.process_run("your-app.exe")  # replace with your app
    try:
        await bot.wait(app, name="Главное окно", timeout_ms=15000)
        await bot.click(app, name="Новый счёт")
        await bot.click(app, name="Сохранить")
    finally:
        await bot.process_stop(app)
    return {"invoice_id": payload["invoice_id"], "posted": True}


async def main() -> None:
    queue = make_queue()
    try:
        report = await run_transactions_async(
            queue,
            process_one,
            queue_name=CONFIG.robot.queue,
            run_id=os.getenv("SMITHCORE_RUN_ID", str(CONFIG.robot.run_id)),
            on_init=dispatcher,
            on_progress=lambda outcome: print(f"{outcome.status}: {outcome.item_id}"),
            stop_checker=lambda: (HERE / "STOP").exists(),
        )
    finally:
        if isinstance(queue, SqliteQueue):
            queue.close()
    print(
        f"Готово: {report.succeeded} ок, {report.business_failed} брак, "
        f"{report.system_failed} сбоев, остановка: {report.stop_reason}"
    )


if __name__ == "__main__":
    asyncio.run(main())
