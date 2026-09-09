# Smithy

Free Python RPA engine — create automation bots with simple async API.

## Quick Start

```python
import asyncio
from smithy import Smithy
from smithy.windows.tools import windows_tools

bot = Smithy(tools=windows_tools())


async def main() -> None:
    app = await bot.process_run("notepad.exe")
    await bot.wait(app, class_name="Notepad", name="*Notepad")
    await bot.click(app, name="File")
    await bot.delay(duration_ms=300)
    await bot.click(app, name="Save As...")
    await bot.input_text(app, text="hello world")
    await bot.keyboard(keys="[CTRL]S")
    await bot.screenshot("notepad.png")
    await bot.process_stop(app)


asyncio.run(main())
```

## Built-in Tools

- **ProcessTool** (`windows.process`) — launch and stop Windows processes by name
- **ClickTool** (`windows.click`) — click a UI element or coordinates; `button` (left/right), `clicks` (1/2)
- **WaitTool** (`windows.wait`) — poll until a UI element appears or disappears (`wait_for`, with timeout)
- **DelayTool** (`windows.delay`) — pause execution for a fixed duration
- **ScreenshotTool** (`windows.screenshot`) — capture the screen or a window to a file
- **InputTextTool** (`windows.input_text`) — type plain text into a UI element
- **KeyboardTool** (`windows.keyboard`) — send key combos and presses (e.g. `"[CTRL]S"`, `"[CTRL!]"`, `"[ENTER]"`)
- **SetTextTool** (`windows.set_text`) — replace a UI element's text programmatically (ValuePattern / WM_SETTEXT)
- **GetElementTool** (`windows.get_element`) — read a UI element's attributes as a dict
- **ScrollTool** (`windows.scroll`) — scroll the wheel over an element or point (`direction`, `wheel_clicks`)
- **HoverTool** (`windows.hover`) — move the mouse over an element (menus, tooltips)
- **ExistsTool** (`windows.exists`) — single-lookup boolean check (no waiting, no raising)
- **GetTextTool** (`windows.get_text`) — read an element's visible text (ValuePattern → Name)
- **WindowTool** (`windows.window`) — activate/minimize/maximize/restore/move/close a window by PID
- **SelectTool** (`windows.select`) — select an item in a dropdown, combobox, or list
- **DragTool** (`windows.drag`) — drag between two endpoints (coordinates or `from_*`/`to_*` selectors)
- **ClipboardTool** (`windows.clipboard`) — read/write clipboard text (needs `pyperclip`)
- **ListElementsTool** (`windows.list_elements`) — list direct children to discover automation IDs
- **HighlightTool** (`windows.highlight`) — flash a colored rectangle for debugging selectors
- **GetTableTool** (`windows.get_table`) — extract DataGrid/ListView/TreeView rows as JSON
- **ControlActionTool** (`windows.control_action`) — native UIA pattern actions (`invoke`, `toggle`, `expand`, `collapse`, `select`, `focus`) that keep working when a window is covered or unfocused
- **FileTool** (`file`) — `read`/`write`/`append`/`copy`/`move`/`delete`/`exists`/`wait_for`/`list`; optional `SMITHY_FILE_ROOT` sandbox confines every path
- **ExcelTool** (`excel`, extra `[excel]`) — `read`/`write`/`append` for xlsx via openpyxl, honors the same file sandbox
- **FindImageTool / ClickImageTool** (`windows.find_image`, `windows.click_image`, extra `[image]`) — OpenCV template matching for UIA-invisible UIs (Citrix/RDP/Java/canvas)
- **OcrTool** (`windows.ocr`) — text from an image file or screen region via the built-in Windows OCR engine, zero extra dependencies

All UI tools accept optional `pid` (or a `ProcessHandle`) to scope element search to a specific window.

`ProcessTool` only starts executables from its allowlist — pass
`windows_tools(allowed_commands=["myapp.exe"])` or set
`SMITHY_ALLOWED_COMMANDS="myapp.exe,other.exe"` to override the demo list.

## Custom Tools

Create tools from simple async functions:

```python
from smithy import Smithy, tool


@tool("greet", description="Greet a person")
async def greet(config: dict) -> dict:
    name = config.get("name", "World")
    return {"message": f"Hello, {name}!"}


bot = Smithy(tools=[greet])


async def main() -> None:
    result = await bot.call("greet", name="Alice")
    print(result["message"])  # Hello, Alice!


asyncio.run(main())
```

## Keyed Selectors (dev capture)

Write bot code with stable *keys* instead of inline selectors, run it in
dev mode, and record each unknown selector interactively — hover the
element, press **CTRL** (ESC cancels). A stored key runs silently; a
missing key or a stale one (`ElementNotFound` mid-run) triggers a
capture, persists it to `selectors.json`, and retries. In production
(no `SMITHY_DEV_CAPTURE`) both fail honestly:

```python
bot = Smithy(tools=windows_tools(), dev_capture=True)
await bot.click(key="login.submit")   # first run: capture; then: silent
await bot.input_text(key="login.password", text=bot.asset("login.password"))
```

Enable dev mode with `dev_capture=True`, the `SMITHY_DEV_CAPTURE=1` env,
or `run_flow --capture` for flows (`key` fields in tool configs work the
same way). Keys never appear in the audit log as resolved fields — the
tracer records them as portable `key` references (see Packs below).

## Packs (dev → delivery)

The delivery unit is a *pack*: a directory (flows, `tools.py`,
`selectors.json`) plus a generated `pack.json` manifest with a SHA-256
per file. Clients refuse to run a tampered bot:

```bash
python -m smithy.pack build bot_dir --name my-bot --version 1.0
python -m smithy.pack verify bot_dir
python -m smithy.pack zip bot_dir --out my-bot.zip
python -m smithy.pack fetch https://cloud.example.com/bot.zip --dest bot_dir
```

One step from dev to the orchestrator — build, verify, zip and upload:

```bash
python -m smithy.pack push bot_dir --name my-bot --version 1.0 \
  --api-url https://cloud.example.com/api
```

`--api-url` defaults to `$SMITHY_API_URL`, the operator token comes from
`$SMITHY_API_TOKEN`. In VSCode, the bundled `.vscode/tasks.json` exposes
this as the default build task (`Ctrl+Shift+B` → "pack: push").

Run a stage straight from the pack (manifest is verified first; `tools.py`
and `selectors.json` are picked up automatically):

```bash
python -m smithy.run_flow --pack bot_dir --stage process
```

The tracer is the dev-side "converter": `Smithy(trace="bot.flow.json")`
records every successful tool call as a v2 `tool` node, keyed calls as
portable `key` references — run your bot script once, feed the resulting
flow document into the pack.

## Transactions (REFramework-style)

The framework owns the Init → Get → Process → SetStatus → End loop over a
queue (local SQLite file or orchestrator via `HttpQueue`):

```python
import asyncio
from smithy import InMemoryQueue, run_transactions_async
from smithy.core.errors import BusinessError

queue = InMemoryQueue()
queue.get_or_create_queue("invoices", max_attempts=3)


async def process(item) -> dict:
    if not item.payload.get("number"):
        raise BusinessError("invoice has no number")  # terminal, no retry
    return {"posted": True}


async def main() -> None:
    report = await run_transactions_async(queue, "invoices", process)
    print(report.processed, report.succeeded, report.business_failed)


asyncio.run(main())
```

`BusinessError` marks an item terminally failed; `InfrastructureError` (or
any unexpected exception) requeues it within the `max_attempts` budget;
`Cancelled` stops the loop cooperatively. Long items get a background
lease heartbeat (capped at 30 minutes). See
[`examples/reframework_bot.py`](examples/reframework_bot.py) for a full
dispatcher + performer skeleton.

## Robot Config (TOML)

One TOML per robot (replaces the two-column Excel sheet), validated up
front — the bot fails in Init, never mid-run:

```python
from smithy import load_config

CONFIG = load_config(
    "reframework_bot.toml",
    required=["robot.queue", "paths.workdir"],
    must_exist=["paths.workdir"],
)
print(CONFIG.robot.queue)  # attribute access, frozen after load
```

Per-environment tweaks without editing TOML via `SMITHY_*` env vars:
`SMITHY_ROBOT__QUEUE=invoices-prod` overrides `robot.queue` (`__` nests,
values are TOML-typed). Secrets never live here — only references to
orchestrator assets. See [`examples/config_demo.py`](examples/config_demo.py).

## Error Handling

```python
from smithy.core.errors import InvalidInput, ElementNotFound, PlatformError

try:
    await bot.click(app, name="Nonexistent")
except ElementNotFound:
    print("Element not found")
except PlatformError as e:
    print(f"Platform error: {e}")
```

## Selector Ranking (Playwright-style)

Record mode (`record` below) ranks every captured element like
Playwright's codegen: candidates in priority order (automation ID →
name + type → class + type), stability scoring, and a live uniqueness
check. The winning selector ships with `high`/`medium`/`low` confidence
plus warnings — `low` means the element needs an anchor, not blind trust:

```python
from smithy.windows.selector_rank import rank_best_selector
from smithy.windows.tools.selector_capture.capture import capture_at_point

_, sel = capture_at_point(400, 300)
ranked = rank_best_selector(sel)
print(ranked.config)  # e.g. {"automation_id": "btnOk"}
print(ranked.confidence, ranked.warnings)
```

`resolve_element(..., strict=True)` fails on ambiguous selectors (2+
matches) instead of taking the first — the desktop equivalent of strict
mode. Numeric control types from real captures (`"50000"`) are
translated to names automatically.

## Selector Capture

A dev utility for inspecting UI elements at screen coordinates and generating tool configs:

```bash
    pip install smithy-engine[capture]

# Single capture mode — one flow node
python -m smithy.windows.tools.selector_capture single -o selectors.json

# Series mode — auto-record clicks and typing
python -m smithy.windows.tools.selector_capture series -o recording.json

# Interactive record mode
python -m smithy.windows.tools.selector_capture record -o flow.json
```

All three modes write the same shape — `{"tool": "selector-capture",
"nodes": [{"tool", "args", "full_path"}]}` (`single` is just a
one-node flow). `args` holds the ranked minimal selector (the
`best_selector` equivalent), `full_path` the full UIA path for debugging
and anchors. Note: series mode records click targets with full paths,
but keyboard input captures only the target element, not the typed text
itself — fill in `text` afterwards or use record mode.

## Codegen (Playwright-style code recording)

Any capture file renders as a replayable bot script — record once, get
runnable code:

```bash
python -m smithy.windows.tools.selector_capture emit -i flow.json -o bot.py

# ...or in one pass, straight from recording:
python -m smithy.windows.tools.selector_capture record -o flow.json --emit bot.py
```

The script uses `Smithy(tools=windows_tools())` with one `await bot.*`
call per node. No magic: the recorder never sees the launched process
(so there's a `TODO` showing `process_run` + PID scoping), uncaptured
`input_text` gets an explicit `text="TODO: fill in"` placeholder, and
fragile selectors ship with `WARNING` comments. Open `bot.py` in your
editor, fill in the TODOs, run.

## Visual Editor

The flow is built in [smithy-designer](https://github.com/as-kurosss/smithy-engine-designer) —
a separate visual editor (MIT): drag-and-drop canvas, step debugger with
breakpoints, XML-like selectors, typed variables.

```bash
pip install smithy-designer
smithy-designer flow.json
```

## Flow format (v2)

The flow file is a versioned JSON document — the contract between the
designer, the file on disk, and the execution engine. The schema lives in
[`schemas/flow-v2.schema.json`](schemas/flow-v2.schema.json).

Compatibility rules:

- **adding optional fields does not bump the version** — unknown keys are
  ignored by older readers (`label`, `breakpoints` were added this way);
- **removing/renaming fields or changing semantics requires v3** and a
  migration path; readers must reject unknown versions with an explicit error;
- the engine and the designer both validate `version` on load and never
  silently overwrite a file of a different version.

Example:

```json
{
  "version": 2,
  "nodes": [
    { "id": "start", "kind": "start", "config": {}, "position": [120, 160] },
    { "id": "a1", "kind": "tool", "tool": "windows.click",
      "config": { "name": "OK", "control_type": "Button" },
      "save_as": "result", "position": [340, 160] }
  ],
  "edges": [
    { "id": "e1", "source": "start", "source_handle": "out", "target": "a1" }
  ]
}
```

### Running a flow

```bash
python -m smithy.run_flow flow.json --set name=value   # exit 0 = finished
```

Exit codes: `0` finished, `1` validation/node failure, `2` stopped
(SIGTERM/Ctrl+C) — a supervising service can distinguish a crash from a
requested stop. Other modes:

```bash
python -m smithy.run_flow flow.json --validate         # dry-run, nothing executes
python -m smithy.run_flow flow.json --vars vars.json --payload item.json
python -m smithy.run_flow flow.json --tools my_tools.py
# REFramework loop over a queue (SQLite or smithy-cloud):
python -m smithy.run_flow flow.json --transactional --queue invoices --db q.db
python -m smithy.run_flow flow.json --transactional --queue invoices --cloud URL --agent ID
```

Or programmatically: `smithy.flow.FlowRunner(registry).run(doc)`.

### Process bundle contract

A flow runs unattended on any orchestrator/agent as a plain Python bundle:

```text
files:        { "flow.json": <v2 doc>, "main.py": <runner shim> }
entry_point:  main.py
requirements: ["smithy-engine[windows]>=0.7"]
```

with the shim being two lines:

```python
from smithy.run_flow import main
sys.exit(main(["flow.json"]))
```

The agent executes it exactly like any other Python program — no orchestrator
changes are needed. `python -m smithy_designer.publish flow.web.json --url
<cloud> --token sct_...` builds and uploads this bundle for you.

## Install

```bash
pip install smithy-engine             # core (no deps)
pip install smithy-engine[windows]     # Windows UIA tools
pip install smithy-engine[capture]     # selector capture (pynput + pyperclip)
pip install smithy-engine[all]         # everything
pip install -e ".[dev]"            # development
```

## Development

```bash
# Using uv (recommended)
uv venv .venv
.venv\Scripts\activate
uv pip install -e ".[dev,windows,capture]"

# Or with pip
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,windows,capture]"

pytest                    # run tests
ruff check src/ tests/    # linter
mypy src/smithy --strict  # type check
```

## Project Structure

```
src/smithy/
├── __init__.py          — Public API: Smithy, ProcessHandle, Tool, errors
├── facade.py            — Smithy facade (async tool dispatch, keyed selectors)
├── flow.py              — FlowRunner (flow-v2 executor: tool/flow/set/if/loop nodes)
├── run_flow.py          — Runner CLI (--set/--vars/--tools/--validate/--pack/--transactional)
├── pack.py              — Packs: manifest build/verify, zip, fetch (SHA-256 integrity)
├── trace.py             — FlowTracer middleware: bot script → flow document
├── core/
│   ├── tool.py          — Tool protocol, AbstractTool, @tool decorator
│   ├── registry.py      — ToolRegistry (name → tool dispatch, schema validation)
│   ├── schema.py        — Hand-rolled JSON Schema subset validator
│   ├── retry.py         — RetryTool (attempts / delay / retry_on)
│   ├── logging.py       — JsonlEventLogger (JSONL audit log middleware)
│   ├── config.py        — TOML robot config + SMITHY_* env overlay
│   ├── assets.py        — AssetProvider protocol, SMITHY_ASSET_* (runtime secrets)
│   ├── files.py         — FileTool (SMITHY_FILE_ROOT sandbox)
│   ├── excel.py         — ExcelTool (openpyxl)
│   ├── queue.py         — Queue protocol, InMemoryQueue, SqliteQueue
│   ├── http_queue.py    — HttpQueue client for the orchestrator
│   ├── transactions.py  — REFramework-style runner + heartbeat
│   ├── events.py        — EventBus, ToolEvent, Middleware
│   ├── selectors.py     — SelectorStore (key → selector registry)
│   └── errors.py        — Error hierarchy (ToolError, ElementNotFound, etc.)
└── windows/
    ├── element.py       — SafeUIElement (thread-safe COM wrapper)
    ├── selector.py      — ElementSelector (UIA tree search + match counting)
    ├── selector_rank.py — Selector ranking (candidates, scoring, confidence)
    └── tools/
        ├── process.py          — ProcessTool (allowlist, wait/status)
        ├── click.py            — ClickTool (button/clicks/coordinates)
        ├── wait.py             — WaitTool (appear/disappear)
        ├── delay.py            — DelayTool
        ├── screenshot.py       — ScreenshotTool
        ├── input_text.py       — InputTextTool
        ├── keyboard.py         — KeyboardTool
        ├── set_text.py         — SetTextTool
        ├── get_element.py      — GetElementTool
        ├── scroll.py           — ScrollTool
        ├── hover.py            — HoverTool
        ├── exists.py           — ExistsTool
        ├── get_text.py         — GetTextTool
        ├── window.py           — WindowTool
        ├── select.py           — SelectTool
        ├── drag.py             — DragTool
        ├── clipboard.py        — ClipboardTool
        ├── list_elements.py    — ListElementsTool
        ├── highlight.py        — HighlightTool
        ├── get_table.py        — GetTableTool (DataGrid/ListView/TreeView → JSON)
        ├── control_action.py   — ControlActionTool (native UIA patterns)
        ├── image.py            — FindImageTool / ClickImageTool (OpenCV)
        ├── ocr.py              — OcrTool (Windows OCR)
        ├── _resolve.py         — Shared element/point resolution helpers
        └── selector_capture/   — Dev tool for UI inspection + codegen
```

## Examples

- [`examples/basic_bot.py`](examples/basic_bot.py) — Launch Notepad and interact with its UI
- [`examples/custom_tool.py`](examples/custom_tool.py) — Create and use custom tools
- [`examples/reframework_bot.py`](examples/reframework_bot.py) — REFramework skeleton: dispatcher + performer over a queue
- [`examples/config_demo.py`](examples/config_demo.py) — Load and validate a TOML robot config

## License

MIT
