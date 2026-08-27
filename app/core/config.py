from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8100


class StorageConfig(BaseModel):
    db_path: str = "./runtime/review.sqlite3"
    data_root: str = "./runtime/data"


class ProjectConfig(BaseModel):
    id: str
    name: str
    enabled: bool = True
    daily_target: int = Field(default=50, ge=1)
    source: dict[str, Any]
    cache: dict[str, Any] = Field(default_factory=dict)
    sink: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    storage: StorageConfig = StorageConfig()
    projects: list[ProjectConfig] = Field(default_factory=list)
    remote_connections: list[dict[str, Any]] = Field(default_factory=list)

    def project(self, project_id: str) -> ProjectConfig:
        for project in self.projects:
            if project.id == project_id:
                return project
        raise KeyError(f"Unknown project: {project_id}")

    def remote_connection(self, connection_id: str) -> dict[str, Any]:
        for connection in self.remote_connections:
            if connection.get("id") == connection_id:
                return connection
        raise KeyError(f"Unknown remote connection: {connection_id}")


def _config_path() -> Path:
    return Path(os.environ.get("REVIEW_CONFIG", "./config/config.json")).resolve()


def load_settings() -> AppConfig:
    path = _config_path()
    if not path.exists():
        example = path.with_name("config.example.json")
        raise FileNotFoundError(
            f"Config not found: {path}. Copy {example} to {path} first."
        )
    return AppConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


settings = load_settings()
