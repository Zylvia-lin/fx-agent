"""Independent case snapshots with bounded copies and explicit verification scope."""

import asyncio
import hashlib
import os
import shutil
import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fx_agent.store import Identifier, compact, scope_id

ROLES = {
    "project": "projects/houdini",
    "reference": "references",
    "preview": "previews",
    "dependency": "dependencies",
}


class ArchiveCancelled(Exception):
    """Cooperative worker cancellation, separate from an asyncio task cancellation."""


def check_cancelled(cancellation: threading.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise ArchiveCancelled("Archive cancelled before publication")


def contained(base: Path, relative: str) -> Path:
    """Resolve an archive member and reject traversal or escaping symlinks."""
    member = Path(relative)
    resolved = (base / member).resolve()
    if member.is_absolute() or ".." in member.parts or not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Invalid archive path: {relative}")
    return resolved


def file_hash(path: Path) -> str:
    """Hash a file without loading it into memory."""
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


def copy_verified(
    source: Path,
    destination: Path,
    *,
    max_bytes: int | None = None,
    cancellation: threading.Event | None = None,
) -> dict:
    """Copy no more than the initial size, rejecting a source changed during copying."""
    digest = hashlib.sha256()
    check_cancelled(cancellation)
    with source.open("rb") as reader, destination.open("xb") as writer:
        before = os.fstat(reader.fileno())
        if max_bytes is not None and before.st_size > max_bytes:
            raise ValueError("Source growth exceeds max_total_bytes")
        remaining = before.st_size
        while remaining:
            check_cancelled(cancellation)
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
        raise ValueError(f"Archive checksum mismatch: {destination}")
    return {"size_bytes": before.st_size, "sha256": digest.hexdigest()}


class ArtifactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    role: Literal["project", "reference", "preview", "dependency"]


class CaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    shot_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    files: list[ArtifactInput] = Field(min_length=1, max_length=500)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, gt=0)


class ArchivedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["project", "reference", "preview", "dependency"]
    source_path: str = Field(min_length=1, max_length=4096)
    archive_path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    case_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    project_id: Identifier
    shot_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    created_at: str = Field(min_length=1)
    archive_state: Literal["provided_files_verified"]
    dependency_scan: Literal["not_performed"]
    reproduction_status: Literal["not_tested"]
    files: list[ArchivedFile] = Field(min_length=1, max_length=500)


async def create_case_async(root: Path, **values) -> dict:
    """Signal cancellation to the copy thread and wait for its staging cleanup."""
    cancellation = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(CaseArchive(root).create, cancellation=cancellation, **values)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancellation.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Repeated stop requests must not abandon the cleanup thread.
                continue
            except ArchiveCancelled:
                break
        with suppress(ArchiveCancelled):
            worker.result()
        raise


class CaseArchive:
    """Publish complete provided-file copies; never claim dependencies were discovered."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cases = contained(self.root, "cases")

    def _case_path(self, case_id: str) -> Path:
        try:
            if uuid.UUID(case_id).hex != case_id:
                raise ValueError("Case IDs must be canonical UUID hex strings")
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid case_id") from exc
        return contained(self.cases, case_id)

    def create(
        self,
        *,
        cancellation: threading.Event | None = None,
        expected_hashes: dict[str, str] | None = None,
        **values,
    ) -> dict:
        """Copy explicitly supplied files; no live HIP save or dependency scan implied."""
        request = CaseInput(**values)
        check_cancelled(cancellation)
        sources = []
        total = 0
        for item in request.files:
            path = Path(item.path).expanduser()
            if not path.is_absolute():
                raise ValueError("Source path must be absolute")
            source = path.resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"Source is not a file: {source}")
            if item.role == "project" and source.suffix.lower() not in (
                ".hip",
                ".hiplc",
                ".hipnc",
                ".nk",
            ):
                raise ValueError("Project must have a .hip, .hiplc, .hipnc or .nk suffix")
            total += source.stat().st_size
            sources.append((item, source))
        if total > request.max_total_bytes:
            raise ValueError(f"Case exceeds max_total_bytes: {total}")
        case_id = uuid.uuid4().hex
        staging = contained(self.cases, f".staging/{case_id}")
        final = self._case_path(case_id)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            members = []
            copied_bytes = 0
            for index, (item, source) in enumerate(sources):
                check_cancelled(cancellation)
                folder = (
                    "projects/nuke"
                    if item.role == "project" and source.suffix.lower() == ".nk"
                    else ROLES[item.role]
                )
                relative = f"{folder}/{index:04d}_{source.name}"
                destination = contained(staging, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Repeat preflight immediately before opening a possibly changed source.
                if copied_bytes + source.stat().st_size > request.max_total_bytes:
                    raise ValueError("Source growth exceeds max_total_bytes")
                info = copy_verified(
                    source,
                    destination,
                    max_bytes=request.max_total_bytes - copied_bytes,
                    cancellation=cancellation,
                )
                if (
                    expected_hashes
                    and str(source) in expected_hashes
                    and info["sha256"] != expected_hashes[str(source)]
                ):
                    raise ValueError(
                        "Source changed during extraction; retry for a consistent snapshot"
                    )
                copied_bytes += info["size_bytes"]
                members.append(
                    {"role": item.role, "source_path": str(source), "archive_path": relative} | info
                )
            manifest = {
                "schema_version": 1,
                "case_id": case_id,
                "project_id": request.project_id,
                "shot_id": request.shot_id,
                "title": request.title,
                "created_at": datetime.now(UTC).isoformat(),
                "archive_state": "provided_files_verified",
                "dependency_scan": "not_performed",
                "reproduction_status": "not_tested",
                "files": members,
            }
            (staging / "manifest.json").write_text(compact(manifest), encoding="utf-8")
            check_cancelled(cancellation)
            publish_directory(staging, final)
        except BaseException:
            # Only delete this call's verified UUID staging directory, never a computed parent.
            if staging.exists():
                checked = contained(self.cases, f".staging/{case_id}")
                if checked != staging or not staging.is_relative_to(self.cases):
                    raise ValueError("Unsafe archive cleanup path") from None
                shutil.rmtree(staging)
            raise
        return manifest | {"case_path": str(final)}

    def get(self, case_id: str, project_id: str) -> dict:
        """Read a published manifest in the requested project scope."""
        folder = self._case_path(case_id)
        manifest = CaseManifest.model_validate_json(
            (folder / "manifest.json").read_text(encoding="utf-8")
        ).model_dump()
        if manifest.get("schema_version") != 1 or manifest.get("case_id") != case_id:
            raise ValueError("Invalid case manifest schema or identity")
        if manifest.get("project_id") != scope_id.validate_python(project_id):
            raise ValueError("Case does not belong to the requested project")
        for item in manifest["files"]:
            contained(folder, item["archive_path"])
        return manifest | {"case_path": str(folder)}

    def summary(self, case_id: str, project_id: str) -> dict:
        """Return a small discovery card; files are expanded only when requested."""
        manifest = self.get(case_id, project_id)
        return {key: value for key, value in manifest.items() if key != "files"} | {
            "file_count": len(manifest["files"])
        }

    def verify(self, case_id: str, project_id: str) -> dict:
        """Check file existence and hashes, not scene opening or visual reproduction."""
        manifest = self.get(case_id, project_id)
        folder = Path(manifest["case_path"])
        issues = []
        for item in manifest["files"]:
            path = contained(folder, item["archive_path"])
            if not path.is_file():
                issues.append({"path": item["archive_path"], "issue": "missing"})
            elif path.stat().st_size != item["size_bytes"] or file_hash(path) != item["sha256"]:
                issues.append({"path": item["archive_path"], "issue": "checksum_mismatch"})
        return {
            "case_id": case_id,
            "valid": not issues,
            "verification_scope": "provided_files_only",
            "reproduction_status": "not_tested",
            "issues": issues,
        }
