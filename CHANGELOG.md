# Changelog

## 0.8.11 - 2026-09-10

### Changed

- **Excel split into three tools** — `excel.read`, `excel.write`,
  `excel.append` (was one `excel` tool with an `action` field), so a flow
  node says exactly what it does. `windows_tools()` now returns 30 tools.
- **Variable names are plain identifiers.** `set.var`, `save_as`,
  `loop.var`/`loop.as`, `variables` names and `flow` `inputs`/`outputs`
  names must match `[A-Za-z_]\w*` — a leading `$` is only the reference
  syntax, never part of a name.

### Added

- **Asset tools** `asset.get` (a text asset) and `asset.credential` (a
  credential's fields). They read the injected `SMITHCORE_ASSET_*` env at run
  time; their values are registered as secrets, so the runner redacts them
  from logs/errors and excludes the variables they land in from the
  run-result snapshot sent back to the orchestrator.
- **On-demand asset resolution by id/GUID or name.** When the agent sets
  `SMITHCORE_ORCHESTRATOR_URL` + `SMITHCORE_AGENT_ID` + a token, the engine uses
  `HttpAssetProvider` and fetches a single asset (`name`, `name.field` or a
  GUID) from `GET /api/agents/{id}/assets/{ref}` instead of the whole vault
  being injected into the environment; otherwise `EnvAssetProvider` is used.
- **Asset fetches are process-scoped.** When `SMITHCORE_PROCESS_ID` is set (the
  agent does it for each run), `HttpAssetProvider` passes it along and the
  orchestrator returns only the assets that process is allowed to read.
- **Typed flow variables** — a flow document's `variables` may be a list of
  `{name, type, value}` (`auto|string|number|bool|json`) as well as the
  plain-object form; `run_flow` seeds defaults with `parse_typed_value`
  before payload / `--set` overrides.

## 0.8.10 - 2026-09-10

Fail node: business vs system failure from a flow.

### Added

- **`fail` node.** `{"kind": "fail", "config": {"mode": "business" | "system",
  "message": "..."}}` ends the run with `BusinessError` (bad data, terminal,
  recorded `business_failed`) or `InfrastructureError` (system failure,
  retried within the queue budget). This makes REFramework-style business
  failures expressible from a flow, not just from Python. Domain errors now
  propagate unwrapped to the transaction runner; the CLI reports them cleanly.

## 0.8.9 - 2026-09-10

Subflows with isolated in/out variables.

### Added

- **`flow` nodes support `"scope": "isolated"`** with `inputs` and
  `outputs`. In isolated mode the child flow gets only the declared
  `inputs` (interpolated in the parent) and returns only the declared
  `outputs` (`{parent: child}` dict, or a list of same-named variables);
  child temporaries no longer leak into the parent. The default scope is
  `"shared"` (previous behaviour) for compatibility.

### Fixed

- **Inline subflow docs are no longer rewritten by parent interpolation.**
  Only `path`/`inputs` are interpolated now; an inline `doc` is evaluated
  in the child scope, so `$var` inside the subflow resolves correctly
  under isolated scope.

## 0.8.8 - 2026-09-10

Template packs: a pack can advertise itself as a reusable blueprint.

### Added

- **`template.json`** — an optional descriptor in a pack folder
  (`title`, `description`, `category`, `icon`, `engine_version`, and
  `params[]` of `string/number/integer/bool/file/folder/asset/choice`).
  `build_pack` validates it and embeds a compact summary under the
  manifest's `template` key, so a catalog can list templates without
  unzipping. Exposed as `smithcore.load_template` / `smithcore.validate_template`.
- **`flow.json` is the preferred main flow name.** When a pack has no
  staged `init/process/end.flow.json`, a single `flow.json` is used as the
  `process` stage — so the entry file reads naturally while the stage
  contract is unchanged.

## 0.8.7 - 2026-09-10

Record → flow: turn a live desktop session into a runnable flow document.

### Added

- **`record_series(stop, on_step=None)`** — a programmatic series recorder
  driven by a `threading.Event` instead of the Ctrl+Shift+F2 hotkey, so a
  server (the designer) can start/stop it over HTTP. Exported from
  `smithcore.windows.tools.selector_capture`.
- **Typed text is captured.** The series listener now carries the typed
  character (`SeriesEvent.char`); printable keys are buffered and flushed
  into a `windows.input_text` node (with the element selector) on the next
  click or on stop. `backspace` edits the buffer. The CLI series mode keeps
  working unchanged.
- **`nodes_to_flow(nodes, name=None)`** — chains recorded `{tool, args}`
  nodes into a flow-v2 document (`start → step N → end`) that the designer
  can open and the engine can replay.

## 0.8.6 - 2026-09-10

A hardening pass after a full re-audit: fixes silent-failure and
security gaps found in the core runtime, the flow runner, pack delivery
and the Windows tools.

### Security

- **`SMITHCORE_ASSET_*` no longer leaks into the robot config.** The env
  overlay now excludes the asset namespace and framework settings, so
  `Config.to_dict()` / `repr(config)` can never expose secrets.
- **`file list` can no longer escape the sandbox.** Glob patterns
  containing `..`, a drive or an absolute root are rejected.
- **`windows.process` path-qualified commands are validated.** A
  path-qualified executable is accepted only when it is the same file a
  bare-name `PATH` lookup finds, so `C:\temp\notepad.exe` cannot shadow
  the allowlisted `notepad.exe`. Stopping by PID now also requires the
  target image name to be allowlisted, and `taskkill` /
  `powershell.exe` are invoked by absolute `System32` path with a
  timeout.
- **`pack` rejects extra files.** Anything on disk but absent from the
  manifest (e.g. a stale/planted `tools.py`) fails verification;
  `fetch_pack` extracts into a fresh staging directory, and manifest
  paths are checked for traversal. HTTP redirects are refused so the
  Bearer token is never replayed to another host.
- **Asset values are redacted from tool error messages** (not just
  config/result), closing a leak through `JsonlEventLogger`.
- **Flow `error` edges now execute**, so a documented recovery branch is
  no longer silently dead; required node fields (`if.condition`,
  `loop.mode`, `set.var`) are validated.

### Fixed

- **`windows.input_text` / `keyboard` actually type.** The `INPUT`
  struct for `SendInput` was 32 bytes instead of the native 40, so every
  `SendInput` call failed with `ERROR_INVALID_PARAMETER` and the error
  was ignored. The struct (with `MOUSEINPUT`) and the return value are
  now correct; keyboard literal segments bypass SendKeys syntax, and the
  extended-key flag is applied only to the navigation cluster.
- **Flow cycles can no longer hang the agent.** The runner enforces a
  step cap, detects nodes without ids and dangling edge targets, and no
  longer reuses a previous run's edges when a document omits them.
- **`control_action` uses real UIA pattern APIs** (`GetInvokePattern`,
  `GetTogglePattern`, `GetExpandCollapsePattern`, `GetSelectionItemPattern`)
  instead of non-existent `Invoke`/`Toggle`/`Expand`/`Collapse` methods.
- **`windows.wait` no longer treats a UIA error as "present"**, and an
  empty selector is rejected instead of matching the first desktop child.
- **`list_elements` / `window` respect COM thread affinity** — all
  control property reads happen on the UIA worker thread; window
  `activate` reports failure, restores minimized windows, and `move`
  keeps Z-order.
- `image` search returns absolute screen coordinates (the region origin
  is added); `get_table` stops walking siblings once `max_rows` is
  reached; OCR sets UTF-8 console encoding; screenshot forces the
  extension to match the requested format.
- `run_flow --transactional` now applies `--set`/`--vars`/`--payload`,
  the pack's `selectors.json` and `--capture`; `--validate` uses the
  pack's selector store and skips schema checks for interpolated fields.
- Core: `blocking` no longer cancels concurrent calls when one times
  out; queues deep-copy payloads, compare SQLite leases with `julianday`,
  and gained `purge_terminal()`; `HttpQueue` URL-encodes ids, caches the
  engine version and wraps malformed responses; `JsonlEventLogger.close()`
  cannot deadlock; `transactions` bounds the async heartbeat join;
  `ExcelTool` no longer leaves an empty default sheet; the tracer caps
  and throttles writes and never bakes PIDs.
- Packaging: added `LICENSE`, fixed the `capture` extra (adds
  `uiautomation`), added console scripts, coverage config, and the
  missing image/OCR tools to the default Windows factory.

## 0.8.5 - 2026-09-10

### Security

- **Asset values are redacted from logs and traces.** Values returned by
  `bot.asset(...)` are now remembered and scrubbed from tool events, so
  the documented `text=bot.asset("login.password")` pattern no longer
  writes the secret into `runs.jsonl` or a traced `flow.json`. The flow
  runner also redacts tool results (not just the config) and set-node
  values before logging.
- **`pack fetch` enforces transport and size limits.** Plain http to a
  non-loopback host is rejected (the in-archive manifest cannot stop a
  MITM who controls both files). Downloads and uncompressed output are
  capped at 200 MiB, defeating zip bombs; archive members with NTFS
  alternate data streams, reserved device names or trailing dots/spaces
  are rejected. `pack push` refuses oversized archives.
- **`windows.process` stop-by-name obeys the allowlist** — a flow can no
  longer terminate arbitrary processes by image name.
- **OCR no longer interpolates the image path/language into the
  PowerShell script**; they are passed via environment variables.

### Changed

- `input_text` types literal text through `SendInput` (Unicode), so
  characters like `{`, `+`, `^`, `%` are no longer interpreted as
  SendKeys control syntax. Astral characters (emoji) are sent as UTF-16
  surrogate pairs.
- `JsonlEventLogger` has a bounded write queue (records are dropped and
  counted under backpressure instead of growing memory) and accepts a
  `redact=` list.
- `FlowRunner` skips config/result JSON serialization when no log sink is
  attached.
- `InMemoryQueue` resets expired leases via a lease heap instead of an
  O(n) scan per claim.
- `screenshot` reuses `core.files.confine_path` instead of duplicating it.
- `smithcore.__version__` is read from package metadata, so it can no longer
  drift from `pyproject.toml`.
- CI: added a coverage gate (>= 70%) and an advisory `pip-audit` job.

### Fixed

- **Packs are flow-only: `smithcore.pack build` never ships `main.py`.** The
  agent runs the flow itself (`smithcore.run_flow`), so the legacy runner
  shim no longer lands in the archive or the orchestrator.
- **`windows.process` stop no longer crashes on localized `taskkill`
  output.** Deployed processes run with `PYTHONUTF8=1`, so strict UTF-8
  decoding of taskkill's OEM-codepage output raised `UnicodeDecodeError`
  in the reader thread; captured output now decodes with
  `errors="replace"`.

## 0.8.4 — 2026-09-10

### Added

- **`smithcore.pack push` — dev → orchestrator in one step:** build + verify
  + zip + upload to smithcore-cloud (`POST /packs/{name}/versions/{version}`)
  in a single command. `--api-url` defaults to `$SMITHCORE_API_URL`, the
  operator token comes from `$SMITHCORE_API_TOKEN` (override with
  `--token-env`); plain http is accepted only for loopback hosts unless
  `--insecure` is passed (same policy as `HttpQueue`). Transient server
  errors (502/503/504) are retried with backoff; 409 surfaces as "bump
  the version" — pack versions are immutable. The bundled
  `.vscode/tasks.json` exposes `pack: push` as the default build task.

## 0.8.3 — 2026-09-09

### Fixed

- **`smithcore.pack build` no longer checksums machine-local junk:** running
  it on a project root that contains a virtualenv (`.venv/`), `.git/`,
  IDE directories or tool caches listed every one of those files in the
  manifest. Environments, VCS and caches are now ignored alongside the
  existing machine-local patterns (`robot.toml`, queues, logs).

## 0.8.2 — 2026-09-09

### Fixed

- **COM apartment lifecycle (crash + silent-failure fix):** UIA elements
  are apartment-bound COM objects; the per-call worker threads created
  them in one thread and consumed them in another (or after that
  apartment was torn down) — producing `E_FAIL` clicks, silent no-op
  `input_text`, "no print output", and access-violation crashes
  (`0xC0000005`) at process exit. All blocking UIA work now runs on a
  single long-lived worker thread that owns one COM apartment
  (`CoInitializeEx` once); UIA elements never cross apartments. A hung
  call abandons the thread and the next call gets a fresh one, so the
  timeout guarantee is preserved.

### Changed

- **Selector ranking early exit:** candidates come in priority order
  with strictly decreasing base scores, so once a unique candidate
  scores above the highest base score of the remaining candidates, the
  (expensive) live desktop walks for them are skipped. Interactive
  capture after CTRL now completes in ~1–2 s instead of ~10 s on busy
  desktops; the ranking result is unchanged.

## 0.8.1 — 2026-09-09

### Fixed

- **dev-capture off the main thread:** `capture_once_async()` crashed
  with `CoInitialize` (`WinError -2147221008`) — UIA/COM apartments are
  per-thread, and `capture_at_point` imports `uiautomation` eagerly (so
  COM was initialized on the main thread only). Interactive capture now
  initializes/uninitializes COM around the UIA walk, so keyed dev
  capture (`bot.click(key=...)` + `SMITHCORE_DEV_CAPTURE=1`) works from
  async bot code.

## 0.8.0 — 2026-09-09

Dev → delivery pipeline: packs, tracer, transactional runs, flow hardening.

### Added

- **`smithcore.pack`** — pack = directory + generated `pack.json` manifest
  (schema `smithcore-pack-v1`) with a SHA-256 per file. Machine-local
  files (`robot.toml`, queues, logs, caches) are deliberately not
  checksummed; the manifest carries the stage → flow entry map
  (init/process/end) and is signature-ready for future signing.
- **CLI**: `python -m smithcore.pack build DIR --name N --version V`
  (entries default to the init/process/end conventions, or pass
  `--entry STAGE=FILE`), `verify DIR`, `zip DIR [--out FILE]` (refuses
  to zip an unverified pack) and `fetch SOURCE --dest DIR` — download a
  pack zip from an ``http(s)://`` URL (or a local path), extract it
  safely (zip-slip rejected: absolute paths, ``..``, drive letters),
  verify the manifest, and only then hand over a ready-to-run
  directory. A tampered archive (any file modified vs its SHA-256) is
  rejected before execution.
- **`run_flow --pack DIR --stage NAME`** — verify the manifest first,
  then run the stage flow from the pack; `tools.py` and
  `selectors.json` are picked up from the pack automatically.
  A tampered file (any listed file modified, missing, or
  unchecksummed entry) makes the runner refuse to start (exit 1).
- **Flow tracer** — `Smithcore(trace="bot.flow.json")`: every successful
  tool call is recorded as a v2 `tool` node; keyed calls are traced as
  `key` (portable selectors), resolved fields are stripped; failed calls
  are not steps. The document is rewritten after every call (crash-safe).
  This is the "converter": bot.py (dev) → flow.json (delivery).
- **`run_flow --vars FILE` / `--payload FILE`** — batch variables
  (JSON object); precedence: flow defaults < payload < vars < `--set`.
- **`run_flow --tools MODULE_OR_PY`** — register custom tools from a
  module (`TOOLS = [...]` convention or module-level `AbstractTool`
  instances). Custom tools ship once with the pack and are the only
  code a client installation ever runs; flows arriving from the cloud
  remain data.
- **`run_flow --transactional`** — REFramework loop over a queue: each
  work item's payload becomes the flow's variables, the flow runs once
  per item, the final variable snapshot is stored as the item result.
  Queue backend: `--db q.db` (local SQLite — full transactional
  resilience without any server) or `--cloud URL --agent ID` with the
  token from `SMITHCORE_CLOUD_TOKEN` (`--insecure` allows plain HTTP).
  Summary line: processed/ok/business/system + stop reason.
- **Node `on_error` policies** (tool and flow nodes): `stop` (default —
  fail the run), `continue` (save `ExceptionType: message` into
  `save_error_as`, default `$_error`, proceed via the `out` handle)
  and `retry` (bounded `retries` with `delay_ms`). `asyncio`
  cancellation always aborts, even under `continue`.
- **`key` in tool configs** — selector resolution through the
  `SelectorStore` (env `SMITHCORE_SELECTOR_STORE`, default
  `selectors.json`). The dev-capture workflow works in flows:
  with `SMITHCORE_DEV_CAPTURE=1` (or `run_flow --capture`) a missing key
  is recorded interactively and a stale one (`ElementNotFound`) is
  re-captured and retried; in production both fail honestly.
- **`${asset:name}` interpolation** in tool configs, `set` values and
  conditions — runtime secrets via an `AssetProvider` (default
  `SMITHCORE_ASSET_*` env). Resolved asset values are redacted from the
  runner's log output.
- **`flow` nodes** (subflows): run a nested document from `config.doc`
  (inline) or `config.path` (file), sharing the variable scope;
  optional `inputs` mapping; recursion capped at 8 levels.
- **`run_flow --validate`** — dry-run: version, start node, unique ids,
  edge endpoints, node shapes, `on_error` specs, tool registration,
  config vs tool schemas, selector-key existence. Nothing executes.
- **Service-grade exit codes** for `run_flow`: `0` finished, `1`
  validation/node failure, `2` stopped (SIGTERM/SIGINT/Ctrl+C cancel
  the run cleanly) — a supervising service can now distinguish a crash
  from a requested stop.
- **`HttpQueue.claim` version stamping** — the claim body carries the
  engine version and the agent version (`SMITHCORE_AGENT_VERSION` env);
  both fields are optional and ignored by servers without the feature.
- Full client-side chain: `fetch → verify → run_flow --pack --stage
  --transactional`. SHA-256 covers integrity (transit + storage);
  authenticity (who signed the pack) is signature-ready in the manifest
  schema and deferred until the cloud launch.

### Deferred

- Manifest signatures (ed25519) — the format already reserves room.
- `TransactionBot` lifecycle sugar (Initialize/Get/Process/Status/End
  hooks), queue `priority` ordering, custom `get_transaction` hooks —
  designed, not built yet.

## 0.7.0 — 2026-09-08

Dev-capture workflow, production toolset, audit hardening, flow-v2
executor and the runner CLI.

### Added

- flow-v2 executor in the engine core (`smithcore.flow.FlowRunner`) and a
  standalone runner CLI: `python -m smithcore.run_flow flow.json [--set NAME=VALUE]`
  — flows from the designer now run as plain programs (e.g. inside
  smithcore-cloud process bundles)
- Programmatic capture API (`smithcore[capture]`):
  `capture_once()` / `capture_once_async()` — block the script, hover
  an element, press CTRL (ESC cancels via `CaptureCancelled`), get a
  ranked `CapturedSelector` (selector + full_path + confidence +
  warnings) back into your code.
- `SelectorStore` (`smithcore.core.selectors`) — key → selector registry
  persisted as JSON (atomic writes, survives corrupt files).
- Facade keyed selectors + dev capture:
  `Smithcore(selector_store=..., dev_capture=True)` (or env
  `SMITHCORE_DEV_CAPTURE=1`) and `key=` on `click`, `wait`, `input_text`,
  `set_text`, `get_element`, `hover`, `exists`, `get_text`,
  `highlight`, `get_table`, `control_action`.
  Workflow: a stored key runs silently (no re-prompting); a missing key
  or a stale one (`ElementNotFound` mid-run) triggers one interactive
  capture, persists it, and retries. In production (dev capture off) a
  missing key is a hard error and a stale selector fails honestly.

  ```python
  bot = Smithcore(tools=windows_tools(), dev_capture=True)
  await bot.click(key="login.submit")   # first run: capture; then: silent
  ```

- `windows.get_table` — extract DataGrid/ListView/TreeView rows as JSON
  (columns from a header control + row arrays); works through wrapper
  containers via bounded descent.
- `windows.control_action` — native UIA pattern actions (`invoke`,
  `toggle`, `expand`, `collapse`, `select`, `focus`) that keep working
  when a window is covered or unfocused (no coordinate clicks).
- `file` tool — `read`/`write`/`append`/`copy`/`move`/`delete`/
  `exists`/`wait_for`/`list`; optional `SMITHCORE_FILE_ROOT` sandbox
  confines every path (flow configs then cannot touch anything outside).
- `excel` tool — `read`/`write`/`append` for xlsx via `openpyxl`
  (new ``excel`` extra), honors the same file sandbox.
- `windows.process` lifecycle: `wait` (bounded wait for exit + exit
  code via Win32 `WaitForSingleObject`) and `status`
  (`running`/`exit_code` via `GetExitCodeProcess`).
- Image fallback for UIA-invisible UIs (Citrix/RDP/Java/canvas):
  `windows.find_image` and `windows.click_image` via OpenCV template
  matching (new ``image`` extra: numpy + opencv-python).
- `windows.ocr` — text from an image file or screen region using the
  built-in Windows OCR engine, zero extra dependencies (Windows
  PowerShell 5.1 WinRT interop); optional `language` (BCP-47).
- Runtime secrets: `smithcore.core.assets` (`AssetProvider` protocol +
  `EnvAssetProvider` over `SMITHCORE_ASSET_*`), `Smithcore(assets=...)` and
  `bot.asset("db.password")`. Values are fetched in bot code and never
  pass through tool configs/results — they cannot leak into the JSONL
  audit log.
- Facade wrappers: `get_table()`, `control_action()`, `process_wait()`,
  `process_status()`, `asset()`; `windows_tools()` now bundles
  `get_table`, `control_action`, `file`, and `excel`.
- `HttpQueue(allow_insecure=True)` — plain-HTTP base URLs are rejected
  unless explicitly allowed (loopback is always permitted); retried
  error responses are closed.
- `SMITHCORE_OUTPUT_ROOT` sandbox for screenshot paths; screenshots opt
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
- Config: `SMITHCORE_BLOCKING_TIMEOUT`/`SMITHCORE_ALLOWED_COMMANDS`/
  `SMITHCORE_OUTPUT_ROOT` no longer leak into the robot config document;
  `Config.__getattr__` no longer risks infinite recursion during
  unpickling.

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
  renders as a `Smithcore(tools=windows_tools())` script with one
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
- `Smithcore` facade methods for every new tool (`scroll`, `hover`,
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
- `smithcore[windows]` extra now includes `pyperclip` (clipboard support).

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
  `SMITHCORE_*` env overlay (`__` nests, TOML-typed values).
- Schema validation (`core/schema.py`): `ToolRegistry.execute` validates
  configs against `schema()` (hand-rolled subset, no new deps).
- Tool-level retries (`core/retry.py`): `RetryTool` wrapper
  (`attempts` / `delay_ms` / `retry_on`, defaults to `ElementNotFound`).
- JSONL audit log (`core/logging.py`): `JsonlEventLogger` middleware with
  `transaction_id`, duration, and error stamped per event.
- `windows_tools()` factory (`windows/tools/__init__.py`): default tool
  set in one call, UIA imports stay lazy.
- `ProcessTool` allowlist is now configurable: constructor param,
  `SMITHCORE_ALLOWED_COMMANDS` env override, `allowed_commands` introspection.
- `parse_control_type()` is public (`windows/selector.py`).
- Examples: `reframework_bot.py` (dispatcher + performer skeleton),
  `config_demo.py` with good/broken TOMLs.

### Changed

- Error model consolidated to the single `ToolError` family; the unused
  legacy `SmithError` / `InvalidParams` / `ContextError` were removed.

## 0.1.1

- Windows UI tools (process, click, wait, delay, screenshot, input_text,
  keyboard, set_text, get_element), selector capture CLI, middleware
  event bus, `@tool` decorator, `Smithcore` facade.
