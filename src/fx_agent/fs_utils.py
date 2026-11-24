"""Safe local filesystem helpers shared by baselines, guides, migration and import."""

import hashlib
import os
import time
from pathlib import Path


def contained(base: Path, relative: str) -> Path:
    """Resolve a member path and reject traversal or escaping symlinks."""
    member = Path(relative)
    if member.is_absolute() or member.drive or ".." in member.parts:
        raise ValueError(f"Invalid relative path: {relative}")
    raw = base / member
    resolved = raw.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Invalid relative path: {relative}")
    return resolved


def file_hash(path: Path) -> str:
    """Hash a file with SHA-256 without loading it into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def publish_directory(stage: Path, destination: Path) -> None:
    """Atomically publish owned staging data, tolerating brief Windows scanner locks."""
    for attempt in range(5):
        try:
            stage.rename(destination)
            return
        except PermissionError as exc:
            if (
                attempt == 4
                or destination.exists()
                or getattr(exc, "winerror", None) not in {5, 32, 33}
            ):
                raise
            time.sleep(0.02 * 2**attempt)


def copy_verified(source: Path, destination: Path, *, max_bytes: int | None = None) -> dict:
    """Copy a file while verifying size and SHA-256 against concurrent source changes."""
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        before = os.fstat(reader.fileno())
        if max_bytes is not None and before.st_size > max_bytes:
            raise ValueError("Source growth exceeds max_bytes")
        remaining = before.st_size
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"Source changed during copy: {source}")
            writer.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(reader.fileno())
        current = source.stat()
        if reader.read(1) or any(
            (before.st_size, before.st_mtime_ns, before.st_ino)
            != (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            for stat in (after, current)
        ):
            raise ValueError(f"Source changed during copy: {source}")
    if file_hash(destination) != digest.hexdigest():
        raise ValueError(f"Checksum mismatch: {destination}")
    return {"size_bytes": before.st_size, "sha256": digest.hexdigest()}
