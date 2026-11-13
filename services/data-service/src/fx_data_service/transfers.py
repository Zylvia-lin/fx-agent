"""Bounded template bundles; client destinations never become server write paths."""

import ntpath
import uuid
import zipfile
from pathlib import Path

from fx_agent.archive import contained, file_hash
from fx_agent.store import compact
from fx_agent.templates import TemplateLibrary

MAX_TRANSFER_BYTES = 512 * 1024 * 1024


def export_bundle(root, study_id, destination):
    if not isinstance(destination, str) or not (Path(destination).is_absolute() or ntpath.isabs(destination)):
        raise ValueError("Destination must be absolute")
    library = TemplateLibrary(root)
    record = library._record(study_id)
    if not library.validate(study_id)["valid"]:
        raise ValueError("Template study checksum validation failed")
    members = record["artifacts"]
    if len(members) > 9999:
        raise ValueError("Export archive exceeds transfer limits")
    case_root = Path(root) / "cases" / record["case_id"]
    folder = Path(root) / "transfers"
    folder.mkdir(exist_ok=True)
    temporary = folder / (".bundle-" + uuid.uuid4().hex)
    target = folder / (uuid.uuid4().hex + ".zip")
    total, seen = 0, set()
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            for member in members:
                name = member["export_path"]
                if "\\" in name or ":" in name or ntpath.isabs(name):
                    raise ValueError("Invalid export archive member")
                contained(folder, name)
                if name in seen:
                    continue
                seen.add(name)
                source = contained(case_root, member["archive_path"])
                if total + source.stat().st_size > MAX_TRANSFER_BYTES:
                    raise ValueError("Export archive exceeds transfer limits")
                # Verify the exact bytes being packaged, not only a prior validation.
                import hashlib
                digest = hashlib.sha256()
                with source.open("rb") as reader, bundle.open(name, "w") as writer:
                    while chunk := reader.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_TRANSFER_BYTES:
                            raise ValueError("Export archive exceeds transfer limits")
                        writer.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != member["sha256"]:
                    raise ValueError("Archive checksum changed during export")
            manifest = record["export_manifest_path"]
            contained(folder, manifest)
            if manifest in seen or "\\" in manifest or ":" in manifest:
                raise ValueError("Invalid export archive manifest")
            metadata = compact(record).encode("utf-8")
            if total + len(metadata) > MAX_TRANSFER_BYTES:
                raise ValueError("Export archive exceeds transfer limits")
            bundle.writestr(manifest, metadata)
        if temporary.stat().st_size > MAX_TRANSFER_BYTES:
            raise ValueError("Export archive exceeds transfer limits")
        temporary.rename(target)
        return {"study_id": study_id, "destination": destination,
                "source_modified": False, "paths_rewritten": False,
                "manifest_member": manifest, "reproduction_status": "not_tested",
                "download": {"path": target.relative_to(root).as_posix(),
                             "sha256": file_hash(target), "size": target.stat().st_size}}
    finally:
        temporary.unlink(missing_ok=True)
