"""Pack — a bot delivery unit with an integrity manifest.

A pack is a directory of files (flows, selectors, custom tools) plus a
generated ``pack.json`` manifest holding a SHA-256 checksum per file.
The runner verifies the manifest before executing anything, so tampering
with the delivered bot is detected on the client.

Deliberately *not* covered by the manifest (machine-local, each client
edits or generates them):

- ``robot.toml`` — per-machine configuration (paths, queue, references)
- ``*.db`` / ``*.jsonl`` / logs / caches — runtime state
- ``pack.json`` itself

Schema: ``smithcore-pack-v1``. The manifest is signature-ready: a future
``signature`` field can cover the whole document without a format change.

Delivery: :func:`zip_pack` archives a built pack, :func:`fetch_pack`
downloads a zip (URL or local path), extracts it safely (zip-slip is
rejected), verifies the manifest, and returns the ready-to-run
directory. :func:`publish_pack` — and the ``push`` CLI subcommand —
does the whole dev→orchestrator release in one step: build → verify →
zip → upload to smithcore-cloud (``POST /packs/{name}/versions/{version}``).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from smithcore.core.errors import InvalidInput

PACK_MANIFEST = "pack.json"
PACK_SCHEMA = "smithcore-pack-v1"
TEMPLATE_FILE = "template.json"
TEMPLATE_SCHEMA = "smithcore-template-v1"
TEMPLATE_PARAM_TYPES = (
    "string",
    "number",
    "integer",
    "bool",
    "file",
    "folder",
    "asset",
    "choice",
)

_RETRYABLE_STATUS: frozenset[int] = frozenset({502, 503, 504})
_RETRY_BASE_SECONDS = 0.5
_MAX_RETRY_BACKOFF_SECONDS = 30.0

#: Hard cap for a downloaded/uploaded pack and for its uncompressed size
#: on disk — a bound against memory exhaustion and zip bombs.
DEFAULT_MAX_PACK_BYTES = 200 * 1024 * 1024

_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

#: Directory names skipped anywhere inside a pack (VCS, envs, caches).
_IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        ".git",
        ".idea",
        ".vscode",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)

#: Machine-local files skipped only at the pack root.
_IGNORED_ROOT_FILES: frozenset[str] = frozenset({PACK_MANIFEST, "robot.toml", "main.py"})

#: Globs skipped anywhere (runtime state, bytecode).
_IGNORED_GLOBS: tuple[str, ...] = ("*.pyc", "*.db", "*.sqlite*", "*.jsonl", "*.log", "*.tmp")

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a Bearer token is never replayed to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect to {newurl!r} is not allowed", headers, fp
        )


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _backoff(attempt: int) -> float:
    return float(min(_RETRY_BASE_SECONDS * 2**attempt, _MAX_RETRY_BACKOFF_SECONDS))


def _is_ignored(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in _IGNORED_DIR_NAMES for part in rel.parts):
        return True
    if rel.as_posix() in _IGNORED_ROOT_FILES:
        return True
    return any(rel.match(pattern) for pattern in _IGNORED_GLOBS)


def _unsafe_rel(rel: str) -> bool:
    """True when a manifest path could escape the pack root."""
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return True
    parts = [part for part in rel.replace("\\", "/").split("/") if part not in ("", ".")]
    return any(part == ".." for part in parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_pack(
    directory: str | Path,
    *,
    name: str,
    version: str,
    entry: dict[str, str] | None = None,
) -> Path:
    """Generate ``pack.json`` for *directory* and return its path.

    Args:
        directory: Pack folder (flows, ``selectors.json``, ``tools.py``, …).
        name: Pack name (e.g. ``"1c-invoices"``).
        version: Pack version (e.g. ``"1.2.0"``).
        entry: Stage → flow file mapping (e.g. ``{"process": "flow.json"}``).
            When omitted, a single ``flow.json`` is used as the ``process``
            stage; otherwise conventional staged files are picked up
            (``init.flow.json`` / ``process.flow.json`` / ``end.flow.json``).

    Raises:
        InvalidInput: If *entry* references missing files, or the
            directory does not exist.
    """
    root = Path(directory)
    if not root.is_dir():
        raise InvalidInput(
            f"pack directory does not exist: {root}", param="directory", input_value=str(root)
        )

    if entry is None:
        entry = {
            stage: f"{stage}.flow.json"
            for stage in ("init", "process", "end")
            if (root / f"{stage}.flow.json").exists()
        }
        # Preferred modern layout: a single ``flow.json`` is the main stage.
        if "process" not in entry and (root / "flow.json").is_file():
            entry["process"] = "flow.json"
    for stage, flow_path in entry.items():
        if _unsafe_rel(str(flow_path)):
            raise InvalidInput(
                f"entry {stage!r} has an unsafe path: {flow_path}",
                param="entry",
                input_value=flow_path,
            )
        if not (root / str(flow_path)).is_file():
            raise InvalidInput(
                f"entry {stage!r} points to a missing file: {flow_path}",
                param="entry",
                input_value=flow_path,
            )

    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not _is_ignored(path, root):
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256(path),
                }
            )

    manifest: dict[str, Any] = {
        "schema": PACK_SCHEMA,
        "name": name,
        "version": version,
        "entry": entry,
        "files": files,
    }
    template = load_template(root)
    if template is not None:
        manifest["template"] = _template_summary(template)
    manifest_path = root / PACK_MANIFEST
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def load_template(directory: str | Path) -> dict[str, Any] | None:
    """Read ``template.json`` from *directory* (``None`` when absent).

    Raises:
        InvalidInput: When the file exists but is not a JSON object or
            fails :func:`validate_template`.
    """
    path = Path(directory) / TEMPLATE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidInput(f"cannot read {TEMPLATE_FILE}: {exc}", param="template") from exc
    if not isinstance(data, dict):
        raise InvalidInput(f"{TEMPLATE_FILE} must be a JSON object", param="template")
    problems = validate_template(data)
    if problems:
        raise InvalidInput(
            f"{TEMPLATE_FILE} is invalid:\n" + "\n".join(f"  - {item}" for item in problems),
            param="template",
        )
    return data


def validate_template(data: dict[str, Any]) -> list[str]:
    """Check a template descriptor; return human-readable problems.

    A template is a pack that advertises the parameters it needs, so a
    catalog can prompt for them. ``title`` is required; each entry of
    ``params`` needs a unique ``name`` and one of :data:`TEMPLATE_PARAM_TYPES`.
    """
    problems: list[str] = []
    schema = data.get("schema")
    if schema is not None and schema != TEMPLATE_SCHEMA:
        problems.append(f"unknown template schema {schema!r}")
    title = data.get("title") or data.get("name")
    if not isinstance(title, str) or not title.strip():
        problems.append("template needs a non-empty 'title'")
    params = data.get("params", [])
    if not isinstance(params, list):
        return problems + ["'params' must be a list"]
    seen: set[str] = set()
    for index, param in enumerate(params):
        if not isinstance(param, dict):
            problems.append(f"param #{index} must be an object")
            continue
        name = param.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"param #{index} needs a non-empty 'name'")
        elif name in seen:
            problems.append(f"duplicate param name {name!r}")
        else:
            seen.add(name)
        ptype = param.get("type")
        if ptype is not None and ptype not in TEMPLATE_PARAM_TYPES:
            problems.append(f"param {name!r}: unknown type {ptype!r}")
    return problems


def _template_summary(template: dict[str, Any]) -> dict[str, Any]:
    """A small catalog entry embedded in ``pack.json`` (no unzip needed)."""
    summary: dict[str, Any] = {}
    for key in ("title", "description", "category", "icon", "engine_version"):
        value = template.get(key)
        if value is not None:
            summary[key] = value
    params = template.get("params")
    if isinstance(params, list):
        summary["params"] = [
            {
                key: param[key]
                for key in ("name", "type", "label", "required", "default")
                if key in param
            }
            for param in params
            if isinstance(param, dict)
        ]
    return summary


def verify_pack(directory: str | Path) -> list[str]:
    """Verify the manifest in *directory*; return a list of problems.

    Empty list = the pack is intact: every listed file exists and its
    checksum matches, every entry points to a checksummed file, the
    manifest schema is known.
    """
    root = Path(directory)
    manifest_path = root / PACK_MANIFEST
    problems: list[str] = []
    if not manifest_path.is_file():
        return [f"no {PACK_MANIFEST} in {root} — build the pack first"]
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest is unreadable: {exc}"]
    if manifest.get("schema") != PACK_SCHEMA:
        problems.append(f"unknown manifest schema: {manifest.get('schema')!r}")
        return problems

    listed: dict[str, str] = {}
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            problems.append("manifest files entry must be an object")
            continue
        rel = str(item.get("path") or "")
        checksum = str(item.get("sha256") or "")
        if _unsafe_rel(rel):
            problems.append(f"manifest entry {rel!r}: unsafe path")
            continue
        if not rel or not _SHA256_RE.match(checksum):
            problems.append(f"manifest entry {rel!r}: missing path or bad sha256")
            continue
        listed[rel] = checksum
        file_path = root / rel
        if not file_path.is_file():
            problems.append(f"listed file is missing: {rel}")
        elif _sha256(file_path) != checksum:
            problems.append(f"checksum mismatch: {rel} (modified after build?)")

    if not listed:
        problems.append("manifest lists no files")

    # Any file present on disk but absent from the manifest is not covered
    # by the integrity check (e.g. a stale/planted tools.py) — reject it.
    on_disk = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not _is_ignored(path, root)
    }
    for rel in sorted(on_disk - set(listed)):
        problems.append(
            f"file present but not in the manifest: {rel} "
            "(stale or planted file — rebuild the pack)"
        )

    for stage, flow_path in (manifest.get("entry") or {}).items():
        if flow_path not in listed:
            problems.append(f"entry {stage!r} file {flow_path!r} is not checksummed")
    return problems


def load_manifest(directory: str | Path) -> dict[str, Any]:
    """Load and structurally verify *directory*'s manifest.

    Returns:
        The manifest document.

    Raises:
        InvalidInput: If verification fails (all problems are listed).
    """
    problems = verify_pack(directory)
    if problems:
        raise InvalidInput(
            f"pack {directory} failed verification:\n"
            + "\n".join(f"  - {item}" for item in problems),
            param="pack",
            input_value=str(directory),
        )
    document: dict[str, Any] = json.loads(
        (Path(directory) / PACK_MANIFEST).read_text(encoding="utf-8")
    )
    return document


# ------------------------------------------------------------------ delivery


def zip_pack(directory: str | Path, *, out: str | Path | None = None) -> Path:
    """Archive a built pack (with manifest) into a zip for delivery.

    Args:
        directory: Built pack directory (must contain a valid manifest).
        out: Target zip path; defaults to ``<name>_<version>.zip``
            next to the directory.

    Returns:
        The zip path.

    Raises:
        InvalidInput: If the pack fails verification.
    """
    root = Path(directory)
    problems = verify_pack(root)
    if problems:
        raise InvalidInput(
            "cannot zip a pack that failed verification:\n"
            + "\n".join(f"  - {item}" for item in problems),
            param="pack",
            input_value=str(root),
        )
    manifest = load_manifest(root)
    if out is None:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{manifest['name']}-{manifest['version']}")
        out = root.parent / f"{safe_name}.zip"
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in manifest["files"]:
            archive.write(root / str(item["path"]), str(item["path"]))
        archive.write(root / PACK_MANIFEST, PACK_MANIFEST)
    return out_path


# ------------------------------------------------------------------ publishing


def _multipart_body(archive: Path, boundary: str) -> Iterator[bytes]:
    """Yield a multipart/form-data body without buffering the whole file."""
    yield (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{archive.name}"\r\n'
        f"Content-Type: application/zip\r\n\r\n"
    ).encode()
    with archive.open("rb") as fh:
        yield from iter(lambda: fh.read(65536), b"")
    yield f"\r\n--{boundary}--\r\n".encode()


def _upload_pack(
    url: str,
    archive: Path,
    *,
    token: str,
    timeout: float = 60.0,
    max_retries: int = 3,
    max_bytes: int = DEFAULT_MAX_PACK_BYTES,
) -> None:
    """Multipart-POST *archive* to the orchestrator (stdlib urllib only).

    The body is streamed in chunks (no 2× file-size buffer) and the
    response is read with a small cap. Retried with exponential backoff
    on connection failures and 502/503/504; meaningful answers
    (400/401/409/413/422) surface immediately with the server's detail.
    Redirects are refused so the Bearer token is never replayed elsewhere.
    """
    size = archive.stat().st_size
    if size > max_bytes:
        raise InvalidInput(
            f"pack archive is {size} bytes, over the {max_bytes}-byte upload cap",
            param="directory",
            input_value=str(archive),
        )
    boundary = uuid.uuid4().hex
    prefix_len = len(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{archive.name}"\r\n'
            f"Content-Type: application/zip\r\n\r\n"
        ).encode()
    )
    suffix_len = len(f"\r\n--{boundary}--\r\n".encode())
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {token}",
        "Content-Length": str(prefix_len + size + suffix_len),
    }
    attempt = 0
    while True:
        request = urllib.request.Request(
            url, data=_multipart_body(archive, boundary), method="POST", headers=headers
        )
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                response.read(4096)
            return
        except urllib.error.HTTPError as exc:
            try:
                if exc.code in _RETRYABLE_STATUS and attempt < max_retries:
                    time.sleep(_backoff(attempt))
                    attempt += 1
                    continue
                detail = exc.read().decode("utf-8", errors="replace")
                message = f"upload to {url} failed with HTTP {exc.code}: {detail}"
                if exc.code == 409:
                    message += " (this name+version already exists — bump the version)"
            finally:
                exc.close()
            raise InvalidInput(message, param="api_url", input_value=url) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            if attempt < max_retries:
                time.sleep(_backoff(attempt))
                attempt += 1
                continue
            raise InvalidInput(
                f"upload to {url} failed: {exc}", param="api_url", input_value=url
            ) from exc


def _check_api_url(base_url: str, *, allow_insecure: bool, label: str = "api_url") -> None:
    """Same transport policy as HttpQueue: https always; loopback http free,
    any other plain http only with *allow_insecure* (token in cleartext)."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise InvalidInput(f"{label} must be an http(s) URL", param=label, input_value=base_url)
    if parsed.scheme != "https" and not allow_insecure:
        host = (parsed.hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise InvalidInput(
                f"{label} must use https:// (pass allow_insecure/--insecure to override)",
                param=label,
                input_value=base_url,
            )


def publish_pack(
    directory: str | Path,
    *,
    name: str,
    version: str,
    api_url: str,
    token: str,
    out: str | Path | None = None,
    entry: dict[str, str] | None = None,
    allow_insecure: bool = False,
    timeout: float = 60.0,
    max_bytes: int = DEFAULT_MAX_PACK_BYTES,
) -> Path:
    """Build, verify, zip and upload a pack — the whole release in one call.

    Args:
        directory: Pack folder (flows, ``selectors.json``, ``tools.py``, …).
        name: Pack name (must match the manifest).
        version: Pack version (must match the manifest).
        api_url: Orchestrator API root, e.g. ``"https://host/api"``.
        token: Bearer token with operator rights.
        out: Optional zip path; defaults to ``<name>_<version>.zip``.
        entry: Optional stage → flow file overrides.
        allow_insecure: Allow plain http to non-loopback hosts.
        timeout: Upload timeout in seconds.
        max_bytes: Reject archives larger than this (memory bound).

    Returns:
        The zip path that was uploaded.

    Raises:
        InvalidInput: On verification or upload failure (409 = version
            already exists — versions are immutable, bump the version).
    """
    root = Path(directory)
    _check_api_url(api_url, allow_insecure=allow_insecure)
    build_pack(root, name=name, version=version, entry=entry)
    archive = zip_pack(root, out=out)
    quoted_name = urllib.parse.quote(name, safe="")
    quoted_version = urllib.parse.quote(version, safe="")
    url = f"{api_url.rstrip('/')}/packs/{quoted_name}/versions/{quoted_version}"
    _upload_pack(url, archive, token=token, timeout=timeout, max_bytes=max_bytes)
    return archive


def _member_parts(name: str) -> list[str]:
    """Validate one archive member name; return its normalized path parts.

    Rejects absolute paths, ``..`` traversal, NTFS alternate data streams
    (``name:stream``) and Windows reserved device names. Raises
    :class:`InvalidInput` on any unsafe member.
    """
    if not name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        raise InvalidInput(f"unsafe path in archive: {name!r}")
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise InvalidInput(f"unsafe path in archive: {name!r}")
    for part in parts:
        if ":" in part:
            raise InvalidInput(f"unsafe path in archive: {name!r} (alternate data stream)")
        if part != part.rstrip(" ."):
            raise InvalidInput(f"unsafe path in archive: {name!r} (trailing dot/space)")
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
            raise InvalidInput(f"unsafe path in archive: {name!r} (reserved Windows name)")
    return parts


def _safe_extract(
    archive: zipfile.ZipFile,
    dest: Path,
    *,
    max_uncompressed: int = DEFAULT_MAX_PACK_BYTES,
) -> None:
    """Extract *archive* into *dest*, rejecting unsafe members (zip-slip).

    Files are streamed one at a time with a running byte cap, so a zip
    bomb cannot exhaust memory or disk. Unsafe member names (absolute,
    traversal, ADS, reserved devices) abort extraction immediately.
    """
    root = dest.resolve()
    written = 0
    for member in archive.infolist():
        parts = _member_parts(member.filename)
        if not parts:
            continue
        target = (root.joinpath(*parts)).resolve()
        if not target.is_relative_to(root):
            raise InvalidInput(f"unsafe path in archive: {member.filename!r}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("wb") as sink:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_uncompressed:
                    raise InvalidInput(
                        f"pack archive expands beyond the allowed size ({max_uncompressed} bytes)"
                    )
                sink.write(chunk)


def _download_to_file(response: Any, target: Path, limit: int) -> None:
    """Stream an HTTP response to *target*, aborting past *limit* bytes."""
    length = response.headers.get("Content-Length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise InvalidInput(
            f"pack download is {length} bytes, over the {limit}-byte cap",
            param="source",
        )
    total = 0
    with target.open("wb") as sink:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise InvalidInput(f"pack download exceeds the {limit}-byte cap", param="source")
            sink.write(chunk)


def manifest_lists_file(manifest: dict[str, Any], rel: str) -> bool:
    """True when *rel* is checksummed in *manifest* (used to gate tools.py)."""
    for item in manifest.get("files") or []:
        if isinstance(item, dict) and str(item.get("path") or "") == rel:
            return True
    return False


def fetch_pack(
    source: str,
    dest: str | Path,
    *,
    allow_insecure: bool = False,
    max_bytes: int = DEFAULT_MAX_PACK_BYTES,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> Path:
    """Download/locate a pack zip, extract and verify it into *dest*.

    *source* is an ``http(s)://`` URL (e.g. served by smithcore-cloud) or a
    local path to a zip. Plain http to a non-loopback host is rejected
    (the manifest inside the archive cannot protect against a MITM who
    controls both files), unless *allow_insecure* is set. Downloads and
    uncompressed output are capped at *max_bytes*. Extraction happens in
    a fresh staging directory that replaces *dest*, so stale or planted
    files (e.g. an old ``tools.py``) never survive. After extraction the
    manifest is verified; a tampered archive raises before anything runs.

    Returns:
        The extracted pack directory (*dest*).

    Raises:
        InvalidInput: On download, archive or verification failure.
    """
    import contextlib

    dest_dir = Path(dest).resolve()
    staging = dest_dir.with_name(f"{dest_dir.name}.staging-{uuid.uuid4().hex[:8]}")
    staging.mkdir(parents=True)
    try:
        if re.match(r"^https?://", source):
            _check_api_url(source, allow_insecure=allow_insecure, label="source")
            tmp_zip = staging / ".pack-download.tmp"
            attempt = 0
            while True:
                try:
                    with _OPENER.open(source, timeout=timeout) as response:
                        _download_to_file(response, tmp_zip, max_bytes)
                    break
                except urllib.error.HTTPError as exc:
                    exc.close()
                    if exc.code in _RETRYABLE_STATUS and attempt < max_retries:
                        time.sleep(_backoff(attempt))
                        attempt += 1
                        continue
                    raise InvalidInput(
                        f"pack download failed with HTTP {exc.code}", param="source"
                    ) from exc
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    http.client.HTTPException,
                ) as exc:
                    if attempt < max_retries:
                        time.sleep(_backoff(attempt))
                        attempt += 1
                        continue
                    raise InvalidInput(
                        f"pack download failed: {exc}", param="source", input_value=source
                    ) from exc
            archive_path: Path = tmp_zip
        else:
            archive_path = Path(source)
            if not archive_path.is_file():
                raise InvalidInput(
                    f"pack archive not found: {source}", param="source", input_value=source
                )
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract(archive, staging, max_uncompressed=max_bytes)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise InvalidInput(
            f"pack archive is not a valid zip: {source}", param="source", input_value=source
        ) from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    problems = verify_pack(staging)
    if problems:
        shutil.rmtree(staging, ignore_errors=True)
        raise InvalidInput(
            "fetched pack failed verification:\n" + "\n".join(f"  - {item}" for item in problems),
            param="pack",
            input_value=str(dest_dir),
        )
    # Replace the destination only after a fully verified extract.
    with contextlib.suppress(OSError):
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
    os.replace(staging, dest_dir)
    return dest_dir


def _parse_entry(items: list[str]) -> dict[str, str] | None:
    if not items:
        return None
    entry: dict[str, str] = {}
    for item in items:
        stage, sep, file = item.partition("=")
        if not sep or not stage or not file:
            raise SystemExit(f"--entry expects STAGE=FILE, got {item!r}")
        entry[stage] = file
    return entry


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(
        prog="smithcore.pack", description="Build and verify bot packs (pack.json manifests)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_entry_arg(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--entry",
            action="append",
            default=[],
            metavar="STAGE=FILE",
            help="stage → flow file (repeatable; defaults to init/process/end conventions)",
        )

    p_build = sub.add_parser("build", help="generate pack.json for a directory")
    p_build.add_argument("directory")
    p_build.add_argument("--name", required=True)
    p_build.add_argument("--version", required=True)
    add_entry_arg(p_build)

    p_verify = sub.add_parser("verify", help="verify a built pack")
    p_verify.add_argument("directory")

    p_zip = sub.add_parser("zip", help="archive a built pack into a zip for delivery")
    p_zip.add_argument("directory")
    p_zip.add_argument("--out", default=None, help="target zip path")

    p_push = sub.add_parser(
        "push",
        help="build + verify + zip + upload to the orchestrator (one step)",
    )
    p_push.add_argument("directory")
    p_push.add_argument("--name", required=True)
    p_push.add_argument("--version", required=True)
    add_entry_arg(p_push)
    p_push.add_argument(
        "--api-url",
        default=os.environ.get("SMITHCORE_API_URL"),
        help="orchestrator API root, e.g. https://host/api (default $SMITHCORE_API_URL)",
    )
    p_push.add_argument(
        "--token-env",
        default="SMITHCORE_API_TOKEN",
        help="env var holding the operator token (default SMITHCORE_API_TOKEN)",
    )
    p_push.add_argument("--out", default=None, help="target zip path")
    p_push.add_argument(
        "--insecure",
        action="store_true",
        help="allow plain http to non-loopback hosts (token travels in cleartext)",
    )

    p_fetch = sub.add_parser("fetch", help="download/locate a pack zip, extract and verify it")
    p_fetch.add_argument("source", help="http(s):// URL or local zip path")
    p_fetch.add_argument("--dest", required=True, help="directory to extract into")
    p_fetch.add_argument(
        "--insecure",
        action="store_true",
        help="allow plain http to non-loopback hosts (manifest verification cannot stop a MITM)",
    )

    args = parser.parse_args(argv)

    if args.command == "build":
        manifest_path = build_pack(
            args.directory,
            name=args.name,
            version=args.version,
            entry=_parse_entry(args.entry),
        )
        print(f"pack built: {manifest_path}")
        return 0

    if args.command == "verify":
        problems = verify_pack(args.directory)
        if problems:
            for problem in problems:
                print(f"problem: {problem}", file=sys.stderr)
            return 1
        print("pack verified: all checksums match")
        return 0

    if args.command == "zip":
        out = zip_pack(args.directory, out=args.out)
        print(f"pack zipped: {out}")
        return 0

    if args.command == "push":
        if not args.api_url:
            print(
                "no orchestrator URL: set --api-url or $SMITHCORE_API_URL "
                "(without it only a local zip is produced)",
                file=sys.stderr,
            )
            return 1
        token = os.environ.get(args.token_env) or ""
        if not token:
            print(f"operator token is empty: set env {args.token_env!r}", file=sys.stderr)
            return 1
        try:
            archive = publish_pack(
                args.directory,
                name=args.name,
                version=args.version,
                api_url=args.api_url,
                token=token,
                out=args.out,
                entry=_parse_entry(args.entry),
                allow_insecure=args.insecure,
            )
        except InvalidInput as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"pack published: {args.name} {args.version} -> {args.api_url} ({archive})")
        return 0

    try:
        fetch_pack(args.source, args.dest, allow_insecure=args.insecure)
    except InvalidInput as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"pack fetched and verified: {args.dest}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
