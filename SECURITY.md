# Security Policy

## Supported versions

The latest tagged release on PyPI (`smithy-engine`) is supported.

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/as-kurosss/smithy-engine/security/advisories/new)
rather than a public issue. Include reproduction steps and the affected
version. You can expect an initial response within a few days.

## Trust model

smithy is an RPA engine: **flow configs and tool configs are trusted code.**
Tools can move the mouse, send keystrokes, read the clipboard, take
screenshots, and (via `windows.process`) start allowlisted executables.
Do not run flow files from untrusted sources, and do not point
`HttpQueue` at servers you do not control.

Guardrails provided by the engine:

- `windows.process` starts only executables matching the
  `SMITHY_ALLOWED_COMMANDS` allowlist (or the built-in default list);
  bare command names are resolved via `PATH`, never the working directory.
  Stopping a process by image *name* is subject to the same allowlist.
- `HttpQueue` refuses plain-HTTP base URLs unless `allow_insecure=True`
  (loopback addresses are always allowed). `pack fetch` applies the same
  policy, caps download and uncompressed sizes, and rejects unsafe archive
  members (traversal, NTFS alternate data streams, reserved device names).
- Screenshot and file paths can be confined with `SMITHY_OUTPUT_ROOT` /
  `SMITHY_FILE_ROOT`.
- Values fetched through `bot.asset(...)` / `${asset:...}` are redacted
  from tool events, the JSONL audit log and traces. Put secrets behind an
  asset reference — inline literals are not tracked.
- The JSONL audit log records tool configs and results by default; use
  `JsonlEventLogger(path, include_config=False, include_result=False)` for
  data that must not be persisted.
