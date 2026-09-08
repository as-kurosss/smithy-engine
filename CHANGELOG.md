# Changelog

## Unreleased

Audit-driven hardening release: correctness, security, resource-leak and
performance fixes across core and windows modules.

### Fixed

- **Correctness:** `_CONTROL_TYPE_MAP` was shifted by one from `"toolbar"`
  onward (and `"text"` alias pointed at `edit`) — `control_type="window"`
  matched SplitButtons, `"pane"` matched Windows, `"text"` matched Edits.
  The table now uses the official UIA ControlTypeIds (incl. new
  `semanticzoom`), pinned by a regression test against
  `uiautomation.ControlType`.
- **Security (windows.process):** allowlist check now uses Windows path
  semantics (`PureWindowsPath`) on every host OS; bare command names are
  resolved via `PATH` only (never the current directory, closing the
  exe-planting hole); `explorer.exe` removed from the default allowlist
  (it accepts arbitrary launch targets as arguments).
- **Resource leaks:** selector-capture recorder listeners are now stopped
  on every exit path (previously every session leaked global keyboard/
  mouse hooks); retried `HTTPError` responses are closed (socket leak);
  `run_blocking` runs each call on a dedicated executor so a hung COM
  call cannot starve the shared pool.
- **Error masking:** `EventBus.emit` isolates middleware — a broken
  middleware is logged and skipped instead of replacing a tool's result
  or exception.
- **Reliability:** transient `claim` failures are retried with
  exponential backoff and counted as system errors instead of killing
  the run; `set_status` transport failures no longer abort the loop
  (item stays `in_progress` until lease expiry); heartbeat `join()` is
  bounded so shutdown cannot stall on a hung HTTP renewal.
- **wait tool:** a persistent UIA failure no longer yields a false
  `disappear=True`/silent `appear` timeout — if no query ever succeeded,
  the tool raises `PlatformError`.
- **highlight:** draws an outline (`NULL_BRUSH`) instead of a solid
  white fill; `duration_ms` capped at 10 s.
- **click:** multi-clicks drop the inter-click wait so the OS recognizes
  double-clicks (default 0.5 s pacing exceeded the double-click time).
- **set_text/input_text:** both fallback errors are reported; raw
  COM/uiautomation exceptions are wrapped into `PlatformError` like in
  sibling tools.

### Added

- `HttpQueue(allow_insecure=True)` — plain-HTTP base URLs are rejected
  unless explicitly allowed (loopback is always permitted); retried
  error responses are closed.
- `SMITHY_OUTPUT_ROOT` sandbox for screenshot paths; screenshots opt
  into per-monitor-v2 DPI awareness (fixes misaligned window captures
  on scaled displays).
- `JsonlEventLogger` writes on a background thread (the event loop is
  never blocked by disk I/O) and supports the context-manager protocol.
- `ElementSelector.from_config()` — shared config→selector building
  (previously duplicated in three places).
- `InMemoryQueue.claim` is O(log n) via a per-queue heap; SQLite
  backend enables WAL + `busy_timeout` and indexes `seq`.
- `RetryTool(jitter=...)` — spread retries out under contention;
  invalid constructor args raise `InvalidInput` (consistent with core).
- `RetryTool`/registry: registering a tool with an empty `schema()`
  emits a `UserWarning` (validation is silently disabled for it).
- Config: `SMITHY_BLOCKING_TIMEOUT`/`SMITHY_ALLOWED_COMMANDS`/
  `SMITHY_OUTPUT_ROOT` no longer leak into the robot config document;
  `Config.__getattr__` no longer risks infinite recursion during
  unpickling.

### Changed

- CI/release workflows pin all actions to commit SHAs; `id-token:
  write` is scoped to the `publish` job only. Dependabot, SECURITY.md
  added; broken `requirements.lock` removed in favor of `uv.lock`;
  generated recorder outputs (`flow.json`, `recording.json`, `bot.py`,
  `test.txt`) untracked.

## 0.5.0

Playwright-style codegen: recorded flows render as runnable bot scripts.

### Added

- Code generation (`windows/tools/selector_capture/emit.py`): any
  capture file (`single`/`series`/`record` — same `nodes` shape)
  renders as a `Smithy(tools=windows_tools())` script with one
  `await bot.*` call per node. New `emit` CLI subcommand
  (`emit -i flow.json -o bot.py [--clip]`) plus `--emit BOT.py` on
  every record mode for one-pass record-to-code.
- Honest placeholders instead of silent gaps: a `TODO` header for the
  unseen app launch (`process_run` + PID scoping), `text="TODO: fill
  in"` for series-mode `input_text` (keys are never captured), and
  `WARNING` comments from static selector-fragility scoring.

## 0.4.1

Unified capture output: every recorder mode writes the same flow shape.

### Changed

- `single` mode now writes `{"tool": "selector-capture", "nodes":
  [...]}` with one ranked node (same shape as `series`/`record`)
  instead of the divergent `captures`/`best_selector` format. The node
  `args` is the ranked minimal selector, `full_path` is attached, and
  numeric control types are translated — previously `best_selector`
  carried the raw `"50011"` the runtime rejects. The `-d`/
  `--description` flag is still accepted but only logged, not persisted.

### Fixed

- Series-mode `windows.input_text` nodes were missing `full_path`
  (keyboard flushes built nodes without the last clicked element's
  path) — now every node carries it. Known limitation, now documented:
  series mode captures the input *target*, not the typed text itself.

## 0.4.0

Playwright-style selector engine for the desktop: ranked selectors with
uniqueness checks and honest confidence instead of all-fields dumps.

### Added

- Selector ranking (`windows/selector_rank.py`): candidate generation in
  priority order (automation ID → name + type → class + type, minimal
  first), static stability scoring (dynamic digits/dates, wildcards, hex
  runs, long names), live-desktop uniqueness check, `high`/`medium`/`low`
  confidence with warnings. Low confidence means "add an anchor", never
  a made-up stable selector.
- `ElementSelector.count_from_desktop(limit)` — bounded tree walk for
  counting matches (strict-mode primitive; dev-time helper, not a runtime
  search path).
- `resolve_element(..., strict=True)` — fail on ambiguous selectors
  (2+ matches → `InvalidInput`) instead of silently taking the first.
- `generate_nodes_from_config()` — flow nodes from an already-ranked
  minimal config.
- Record mode now ranks every capture: logs the winning selector,
  confidence, and warnings, and emits nodes from the ranked config
  (falls back to the unranked dump if the UIA walk fails).

### Fixed

- Real captures carry numeric control types (`"50000"`) which the
  runtime rejects — `build_inline_selector` now translates them to names
  (`"button"`) and drops untranslatable ones.

## 0.3.0

GUI batch: comfortable desktop automation on top of the 0.2.0 core.

### Added

- Ten new Windows tools: `windows.scroll` (wheel over element/point),
  `windows.hover` (menus, tooltips), `windows.exists` (single-lookup
  boolean), `windows.get_text` (ValuePattern → Name fallback),
  `windows.window` (activate/minimize/maximize/restore/move/close by PID
  via Win32), `windows.select` (dropdown/combobox/list via
  SelectionItemPattern), `windows.drag` (two endpoints, coordinates or
  `from_*`/`to_*` selectors), `windows.clipboard` (get/set via
  `pyperclip`), `windows.list_elements` (direct-children dump for
  discovering automation IDs), `windows.highlight` (colored rectangle
  flash for debugging selectors).
- `Smithy` facade methods for every new tool (`scroll`, `hover`,
  `exists`, `get_text`, `window`, `select`, `drag`, `clipboard`,
  `list_elements`, `highlight`), all accepting an optional `handle` for
  PID scoping.
- Shared `_resolve.py` helpers: `resolve_point` (coordinates win over
  selectors) and `resolve_element`.

### Changed

- `windows.click` now takes `button` (left/right), `clicks` (1/2), and
  `x`/`y` coordinate clicks (double right-click = two `RightClick`
  calls; no module-level `DoubleClick` exists in `uiautomation`).
- `windows.wait` now takes `wait_for` (`appear`/`disappear`) with a
  symmetric poll loop; `PlatformError` mid-poll counts as still present.
- `smithy[windows]` extra now includes `pyperclip` (clipboard support).

## 0.2.0

First minor release: transactions, config, and hardening on top of the
0.1.x tool core.

### Added

- Transactional queue model (`core/queue.py`): `Queue` protocol,
  `InMemoryQueue` / `SqliteQueue` with atomic FIFO claim, lease expiry,
  `max_attempts` requeue, idempotent add, `run_id` ownership; `HttpQueue`
  client for the orchestrator (stdlib only, retries on 502–504).
- REFramework-style runner (`core/transactions.py`): `run_transactions` /
  `run_transactions_async`, `BusinessError` vs `InfrastructureError`
  contract, `Cancelled` cooperative stop, background lease heartbeat
  (capped at 30 min), `on_progress` hook, `TransactionReport`.
- TOML robot config (`core/config.py`): `load_config` with fail-fast
  validation (`required` / `must_exist`), frozen attribute-style `Config`,
  `SMITHY_*` env overlay (`__` nests, TOML-typed values).
- Schema validation (`core/schema.py`): `ToolRegistry.execute` validates
  configs against `schema()` (hand-rolled subset, no new deps).
- Tool-level retries (`core/retry.py`): `RetryTool` wrapper
  (`attempts` / `delay_ms` / `retry_on`, defaults to `ElementNotFound`).
- JSONL audit log (`core/logging.py`): `JsonlEventLogger` middleware with
  `transaction_id`, duration, and error stamped per event.
- `windows_tools()` factory (`windows/tools/__init__.py`): default tool
  set in one call, UIA imports stay lazy.
- `ProcessTool` allowlist is now configurable: constructor param,
  `SMITHY_ALLOWED_COMMANDS` env override, `allowed_commands` introspection.
- `parse_control_type()` is public (`windows/selector.py`).
- Examples: `reframework_bot.py` (dispatcher + performer skeleton),
  `config_demo.py` with good/broken TOMLs.

### Changed

- Error model consolidated to the single `ToolError` family; the unused
  legacy `SmithError` / `InvalidParams` / `ContextError` were removed.

## 0.1.1

- Windows UI tools (process, click, wait, delay, screenshot, input_text,
  keyboard, set_text, get_element), selector capture CLI, middleware
  event bus, `@tool` decorator, `Smithy` facade.
