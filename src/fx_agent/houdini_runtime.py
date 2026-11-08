"""Build an immutable Houdini plugin runtime with the maintained startup fix."""

import hashlib
import json
import shutil
import tempfile
from importlib.metadata import version
from pathlib import Path

STARTUP = Path("scripts/python/fxhoudinimcp_server/startup.py")


def upstream_plugin() -> Path:
    from fxhoudinimcp.houdini_package import package_json

    package = json.loads(package_json())
    env = {key: value for item in package["env"] for key, value in item.items()}
    return Path(env["FXHOUDINIMCP"])


def runtime_path(root: Path, source: Path) -> Path:
    """Pure preview: no runtime/profile writes until installation is applied."""
    upstream_version = version("fxhoudinimcp")
    if upstream_version != "2.25.0":
        raise ValueError(
            "Revalidate the Houdini startup adapter before changing fxhoudinimcp 2.25.0"
        )
    patch = root / "integrations/houdini/startup/startup.py"
    digest = hashlib.sha256(
        upstream_version.encode() + (source / STARTUP).read_bytes() + patch.read_bytes()
    ).hexdigest()[:20]
    return root / ".runtime/houdini" / digest


def prepare_runtime(root: Path, source: Path) -> Path:
    """Leave site-packages and existing running runtimes untouched."""
    destination = runtime_path(root, source)
    patch = root / "integrations/houdini/startup/startup.py"
    if destination.exists():
        if (destination / STARTUP).read_bytes() != patch.read_bytes():
            raise ValueError(f"Houdini runtime was modified: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "plugin"
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(patch, staged / STARTUP)
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(patch.parent / name, staged / f"STARTUP-{name}")
        try:
            staged.rename(destination)
        except FileExistsError:
            if (destination / STARTUP).read_bytes() != patch.read_bytes():
                raise ValueError(f"Conflicting Houdini runtime: {destination}") from None
    return destination
