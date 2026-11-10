"""Dataset routing: default keeps every existing row and archive in place."""

import hashlib

from fx_agent.data_context import service_scope
from fx_agent.store import Store


class DataProjects:
    def __init__(self, settings, backend):
        self.settings, self.backend = settings, backend
        self.cache = {"default": (settings, backend)}

    def get(self, project):
        if project not in self.cache:
            digest = hashlib.sha256(project.encode()).hexdigest()[:32]
            settings = self.settings.model_copy(
                update={
                    "data_root": self.settings.database_root / "projects" / digest / "data",
                    "database_root": self.settings.database_root / "projects" / digest / "database",
                    "allowed_roots": [],
                }
            )
            if (
                self.settings.database_root.is_relative_to(self.settings.data_root)
                or self.settings.data_root.is_relative_to(self.settings.database_root)
            ):
                raise ValueError("Multiple data projects require disjoint data and database roots")
            backend = self.backend.for_project(project) if self.backend else None
            if backend:
                backend.initialize()
            with service_scope(settings.data_root, settings.database_root, "", backend):
                Store(settings.data_root)
            self.cache[project] = settings, backend
        return self.cache[project]

    @staticmethod
    def device_key(project, device):
        # Preserve existing default-device ownership and request replay records.
        if project == "default":
            return device
        # NUL cannot appear in a validated external device ID, so even a forged
        # default-device ID cannot collide with a different space's cache key.
        return "\x00space:" + hashlib.sha256(project.encode()).hexdigest()[:32] + ":" + device
