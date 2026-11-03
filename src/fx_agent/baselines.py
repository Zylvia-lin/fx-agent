"""Verified, immutable rule snapshots carried inside data rather than only a venv."""

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from importlib.metadata import distribution
from pathlib import Path

from fx_agent import __version__
from fx_agent.archive import contained, copy_verified, file_hash, publish_directory
from fx_agent.store import compact


def _identity(manifest: dict) -> str:
    return hashlib.sha256(
        compact({k: v for k, v in manifest.items() if k != "snapshot_id"}).encode()
    ).hexdigest()


def validate_baseline(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{0,63}|[a-f0-9]{64}", folder.name)
        or manifest.get("schema_version") != 1
        or manifest.get("source") not in {"fxhoudinimcp", "fx-agent"}
        or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("snapshot_id", "")))
        or _identity(manifest) != manifest["snapshot_id"]
    ):
        raise ValueError("Invalid baseline manifest identity")
    paths = set()
    for item in manifest["files"]:
        path = contained(folder, item["path"])
        if (
            item["path"] in paths
            or path.is_symlink()
            or path.is_junction()
            or not path.is_file()
            or path.stat().st_size != item["size_bytes"]
            or file_hash(path) != item["sha256"]
        ):
            raise ValueError("Module baseline checksum validation failed")
        paths.add(item["path"])
    actual = {
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and p != folder / "manifest.json"
    }
    if actual != paths:
        raise ValueError("Module baseline contains unlisted files")
    return manifest


def baseline_status(root: Path) -> dict:
    root = Path(root).resolve()
    base = contained(root, "baselines")
    snapshots = []
    for folder in sorted(base.iterdir()) if base.exists() else []:
        if folder.name == ".staging":
            if any(folder.iterdir()):
                raise ValueError("Module baseline snapshot is still being written")
            continue
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise ValueError("Invalid baseline snapshot directory")
        manifest = validate_baseline(folder)
        snapshots.append(
            {k: manifest[k] for k in ("snapshot_id", "source", "version", "workflow_count")}
            | {"file_count": len(manifest["files"]), "relative_path": "baselines/" + folder.name}
        )
    return {"snapshots": snapshots}


def validate_baselines(root: Path) -> dict:
    try:
        return baseline_status(root) | {"valid": True}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "error": str(exc)}


@contextmanager
def baseline_lock(root: Path):
    """Serialize snapshot publication without creating or upgrading the memory DB."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".baseline.lock"
    deadline = time.monotonic() + 30
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ValueError(
                    "Baseline writer is busy; verify no writer is running before removing a stale .baseline.lock"
                ) from None
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def _short_name(source: str, used: set[str]) -> str:
    word = "houdini" if source == "fxhoudinimcp" else "project"
    name, number = word, 2
    while name in used:
        name, number = f"{word}-{number}", number + 1
    return name


def baseline_destination(root: Path, manifest: dict, reserved: set[str]) -> tuple[Path, bool]:
    snapshots = baseline_status(root)["snapshots"]
    for snapshot in snapshots:
        if snapshot["snapshot_id"] == manifest["snapshot_id"]:
            return contained(root, snapshot["relative_path"]), True
    used = {Path(s["relative_path"]).name for s in snapshots} | reserved
    name = _short_name(manifest["source"], used)
    reserved.add(name)
    return contained(root, "baselines/" + name), False


def _shorten_legacy_paths(root: Path) -> None:
    snapshots = baseline_status(root)["snapshots"]
    used = {Path(s["relative_path"]).name for s in snapshots}
    for snapshot in snapshots:
        old = contained(root, snapshot["relative_path"])
        if re.fullmatch(r"[a-f0-9]{64}", old.name):
            name = _short_name(snapshot["source"], used)
            used.add(name)
            target = contained(root, "baselines/" + name)
            publish_directory(old, target)


def write_baseline_indexes(root: Path) -> None:
    root = Path(root).resolve()
    snapshots = baseline_status(root)["snapshots"]
    for software in ("houdini", "nuke"):
        relative = f"modules/global/{software}/README.md"
        path = contained(root, relative)
        if path != root / relative:
            raise ValueError("Module index paths must not be links")
        lines = [
            f"# {software} 基础规范与学习补充",
            "",
            "基础规范快照和个人学习补充分开保存，均随整个 data 目录迁移。",
            "",
            "## 基础规范快照",
            "",
        ]
        for snapshot in snapshots:
            if (
                software == "houdini"
                and snapshot["source"] != "fxhoudinimcp"
                or software == "nuke"
                and snapshot["source"] != "fx-agent"
            ):
                continue
            target = "../../../" + snapshot["relative_path"]
            lines += [
                f"- [{snapshot['source']} {snapshot['version']}]({target}/manifest.json)：{snapshot['file_count']}个文件，{snapshot['workflow_count']}份工作流。"
            ]
            if software == "houdini":
                lines += [
                    f"  [Pyro原始规范]({target}/markdown/workflows/pyro.md)；其他模块见快照目录。"
                ]
            else:
                lines += [f"  [Nuke工作流]({target}/skills/nuke-workflow/SKILL.md)。"]
        lines += [
            "",
            "## 个人学习补充",
            "",
            "当前目录下的模块MD由已确认、已归档并最终提炼的经验生成。没有符合条件的经验时，不生成该模块的补充文件；README存在不代表已经学到成功方案。",
            "",
            "原始上游文档可能带模板占位符，运行时格式化仍由对应版本的get_workflow_guide完成。快照保留版本、许可证和SHA-256，不替代个人经验，也不被Dream改写。",
            "",
            "本文件自动维护；人工笔记请另建文件。",
            "",
        ]
        content = "\n".join(lines)
        # Preserve a pre-existing user README; do not silently replace their notes.
        if path.exists() and not path.read_text(encoding="utf-8").startswith(
            f"# {software} 基础规范与学习补充\n"
        ):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def sync_baselines(root: Path) -> dict:
    root = Path(root).resolve()
    with baseline_lock(root):
        return _sync_baselines(root)


def _sync_baselines(root: Path) -> dict:
    existing = validate_baselines(root)
    if not existing["valid"]:
        raise ValueError("Existing baseline failed validation: " + existing["error"])
    _shorten_legacy_paths(root)
    package = distribution("fxhoudinimcp")
    markdown = Path(package.locate_file("fxhoudinimcp/prompts/markdown"))
    upstream = [
        (p, "markdown/" + p.relative_to(markdown).as_posix())
        for p in sorted(markdown.rglob("*.md"))
    ]
    licenses = [
        (Path(package.locate_file(p)), "licenses/" + Path(str(p)).name)
        for p in package.files or []
        if "/licenses/" in str(p).replace("\\", "/") and Path(str(p)).name in {"LICENSE", "NOTICE"}
    ]
    if not upstream or {name for _, name in licenses} != {"licenses/LICENSE", "licenses/NOTICE"}:
        raise ValueError("Installed upstream baseline or license files are incomplete")
    sources = [("fxhoudinimcp", package.version, upstream + licenses)]
    project = Path(__file__).resolve().parents[2]
    skills = [
        (project / "skills" / name / "SKILL.md", f"skills/{name}/SKILL.md")
        for name in ("fx-workflow", "fx-learning", "nuke-workflow", "fx-project-learn")
    ]
    if all(path.is_file() for path, _ in skills):
        sources.append(("fx-agent", __version__, skills))
    base = contained(root, "baselines")
    if base != root / "baselines":
        raise ValueError("Baseline directories must not be links")
    staging = contained(base, ".staging")
    staging.mkdir(parents=True, exist_ok=True)
    for source, version, files in sources:
        entries = [
            {"path": relative, "size_bytes": path.stat().st_size, "sha256": file_hash(path)}
            for path, relative in files
        ]
        manifest = {
            "schema_version": 1,
            "source": source,
            "version": version,
            "workflow_count": sum("/workflows/" in f["path"] for f in entries)
            if source == "fxhoudinimcp"
            else len(skills),
            "files": entries,
        }
        manifest["snapshot_id"] = _identity(manifest)
        final, exists = baseline_destination(root, manifest, set())
        if exists:
            continue
        with tempfile.TemporaryDirectory(prefix="baseline-", dir=staging) as work:
            stage = Path(work) / final.name
            stage.mkdir()
            for (path, relative), expected in zip(files, entries, strict=True):
                target = contained(stage, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                copied = copy_verified(path, target)
                if copied["sha256"] != expected["sha256"]:
                    raise ValueError("Module baseline changed during snapshot")
            (stage / "manifest.json").write_text(compact(manifest), encoding="utf-8")
            validate_baseline(stage)
            if final.exists():
                if validate_baseline(final)["snapshot_id"] != manifest["snapshot_id"]:
                    raise ValueError("Baseline destination changed during publication")
            else:
                publish_directory(stage, final)
    write_baseline_indexes(root)
    return baseline_status(root) | {"originals_modified": False}
