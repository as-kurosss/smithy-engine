# Security Policy

## Supported versions

The latest tagged release on PyPI (`smithcore-engine`) is supported.

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/as-kurosss/smithcore-engine/security/advisories/new)
rather than a public issue. Include reproduction steps and the affected
version. You can expect an initial response within a few days.

## Trust model

smithcore is an RPA engine: **flow configs, tool configs and custom
`tools.py` modules are trusted code.** Tools can move the mouse, send
keystrokes, read the clipboard, take screenshots, and (via
`windows.process`) start allowlisted executables. Packs are
integrity-checked with SHA-256 but **not signed** (see the changelog),
so only fetch them from an orchestrator you control. Do not run flow
files from untrusted sources, and do not point `HttpQueue` at servers you
do not control.

Guardrails provided by the engine:

- `windows.process` starts only executables matching the
  `SMITHCORE_ALLOWED_COMMANDS` allowlist (or the built-in default list);
  bare command names are resolved via `PATH`, never the working directory,
  and a path-qualified command is accepted only when it is the same file
  PATH resolution finds. Stopping by image *name* uses the same allowlist;
  stopping by *PID* is allowed only when the target's image name is in the
  allowlist.
- `HttpQueue` refuses plain-HTTP base URLs unless `allow_insecure=True`
  (loopback addresses are always allowed). `pack fetch` applies the same
  policy, caps download and uncompressed sizes, rejects unsafe archive
  members (traversal, NTFS alternate data streams, reserved device names),
  extracts into a fresh staging directory, refuses extra files not listed
  in the manifest, and does not follow redirects (so the Bearer token is
  never replayed to another host).
- Robot `config` files never hold secrets: `SMITHCORE_ASSET_*` (and other
  framework `SMITHCORE_*` settings) are excluded from the env overlay, so
  they cannot leak through `Config.to_dict()` / `repr()`.
- Screenshot and file paths can be confined with `SMITHCORE_OUTPUT_ROOT` /
  `SMITHCORE_FILE_ROOT`.
- Values fetched through `bot.asset(...)` / `${asset:...}` are redacted
  from tool configs, results, error messages, the JSONL audit log and
  traces. Put secrets behind an asset reference — inline literals are not
  tracked.
- The JSONL audit log records tool configs and results by default; use
  `JsonlEventLogger(path, include_config=False, include_result=False)` for
  data that must not be persisted.
