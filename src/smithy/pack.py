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

Schema: ``smithy-pack-v1``. The manifest is signature-ready: a future
``signature`` field can cover the whole document without a format change.

Delivery: :func:`zip_pack` archives a built pack, :func:`fetch_pack`
downloads a zip (URL or local path), extracts it safely (zip-slip is
rejected), verifies the manifest, and returns the ready-to-run
directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from smithy.core.errors import InvalidInput

PACK_MANIFEST = "pack.json"
PACK_SCHEMA = "smithy-pack-v1"

#: Machine-local/runtime files — never checksummed.
_IGNORED_PATTERNS: tuple[str, ...] = (
    PACK_MANIFEST,
    "robot.toml",
    "__pycache__",
    "*.db",
    "*.sqlite*",
    "*.jsonl",
    "*.log",
    "*.tmp",
)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _is_ignored(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return any(pattern in path.parts or rel.match(pattern) for pattern in _IGNORED_PATTERNS)


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
        entry: Stage → flow file mapping (e.g. ``{"process": "process.flow.json"}``).
            When omitted, conventional files are picked up automatically
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
    for stage, flow_path in entry.items():
        if not (root / flow_path).is_file():
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
    manifest_path = root / PACK_MANIFEST
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


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


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """Extract *archive* into *dest*, rejecting unsafe member paths (zip-slip)."""
    root = dest.resolve()
    for member in archive.infolist():
        name = member.filename
        if not name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
            raise InvalidInput(f"unsafe path in archive: {name!r}")
        parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise InvalidInput(f"unsafe path in archive: {name!r}")
        target = (root / Path(*parts)).resolve()
        if not target.is_relative_to(root):
            raise InvalidInput(f"unsafe path in archive: {name!r}")
    archive.extractall(dest)


def fetch_pack(source: str, dest: str | Path) -> Path:
    """Download/locate a pack zip, extract and verify it into *dest*.

    *source* is an ``http(s)://`` URL (e.g. served by smithy-cloud) or a
    local path to a zip. After extraction the manifest is verified; a
    tampered archive raises before anything runs.

    Returns:
        The extracted pack directory (*dest*).

    Raises:
        InvalidInput: On download, archive or verification failure.
    """
    import contextlib

    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip: Path | None = None
    try:
        if re.match(r"^https?://", source):
            with urllib.request.urlopen(source, timeout=60) as response:  # noqa: S310 — https enforced by https-only policy, loopback http allowed
                data = response.read()
            tmp_zip = dest_dir / ".pack-download.tmp"
            tmp_zip.write_bytes(data)
            archive_path: Path = tmp_zip
        else:
            archive_path = Path(source)
            if not archive_path.is_file():
                raise InvalidInput(
                    f"pack archive not found: {source}", param="source", input_value=source
                )
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract(archive, dest_dir)
    except zipfile.BadZipFile as exc:
        raise InvalidInput(
            f"pack archive is not a valid zip: {source}", param="source", input_value=source
        ) from exc
    finally:
        if tmp_zip is not None:
            with contextlib.suppress(OSError):
                tmp_zip.unlink()

    problems = verify_pack(dest_dir)
    if problems:
        raise InvalidInput(
            "fetched pack failed verification:\n" + "\n".join(f"  - {item}" for item in problems),
            param="pack",
            input_value=str(dest_dir),
        )
    return dest_dir


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="smithy.pack", description="Build and verify bot packs (pack.json manifests)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="generate pack.json for a directory")
    p_build.add_argument("directory")
    p_build.add_argument("--name", required=True)
    p_build.add_argument("--version", required=True)
    p_build.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="STAGE=FILE",
        help="stage → flow file (repeatable; defaults to init/process/end conventions)",
    )

    p_verify = sub.add_parser("verify", help="verify a built pack")
    p_verify.add_argument("directory")

    p_zip = sub.add_parser("zip", help="archive a built pack into a zip for delivery")
    p_zip.add_argument("directory")
    p_zip.add_argument("--out", default=None, help="target zip path")

    p_fetch = sub.add_parser("fetch", help="download/locate a pack zip, extract and verify it")
    p_fetch.add_argument("source", help="http(s):// URL or local zip path")
    p_fetch.add_argument("--dest", required=True, help="directory to extract into")

    args = parser.parse_args(argv)

    if args.command == "build":
        entry: dict[str, str] | None = None
        if args.entry:
            entry = {}
            for item in args.entry:
                stage, sep, file = item.partition("=")
                if not sep or not stage or not file:
                    print(f"--entry expects STAGE=FILE, got {item!r}", file=sys.stderr)
                    return 1
                entry[stage] = file
        manifest_path = build_pack(
            args.directory, name=args.name, version=args.version, entry=entry
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

    try:
        fetch_pack(args.source, args.dest)
    except InvalidInput as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"pack fetched and verified: {args.dest}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
