"""Bounded file publication, with private staging and original-byte retrieval.

The process supervising an execution owns publication. Workers only create
staged files, so killing or rolling back a worker cannot expose partial output.
This shares the executor's trusted-operator boundary, not a tenant sandbox.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from code_executor.models.settings import Settings

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")


def valid_id(value: str) -> bool:
    return bool(_ID.fullmatch(value)) and ".." not in value


def safe_filename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:200]
    return name if name not in {"", ".", ".."} else "download"


class FileArtifacts:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = Path(settings.artifacts_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in (".staging", ".files", ".manifests"):
            path = self.root / name
            if path.is_symlink():
                raise ValueError("Artifact storage directories cannot be symlinks")
            path.mkdir(exist_ok=True)

    def stage_dir(self, execution_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
            raise ValueError("Invalid execution id")
        path = self.root / ".staging" / execution_id
        if path.is_symlink():
            raise ValueError("Invalid staging directory")
        return path

    def save(
        self,
        execution_id: str,
        data: Any,
        *,
        filename: str,
        mime_type: str | None = None,
        source: dict | None = None,
    ) -> dict[str, Any]:
        if not isinstance(filename, str):
            raise TypeError("filename must be a string")
        filename = safe_filename(filename)
        mime = (
            mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        if (
            not isinstance(mime, str)
            or len(mime) > 200
            or not re.fullmatch(r"[\x20-\x7e]+", mime)
        ):
            raise ValueError("Invalid MIME type")
        if isinstance(data, str):
            data = io.BytesIO(data.encode("utf-8"))
        elif isinstance(data, bytes):
            data = io.BytesIO(data)
        elif not callable(getattr(data, "read", None)):
            raise TypeError(
                "save_artifact expects bytes, text, or an opened binary stream"
            )
        stage = self.stage_dir(execution_id)
        stage.mkdir(exist_ok=True)
        existing = [p for p in stage.iterdir() if not p.name.startswith(".")]
        if len(existing) >= self.settings.artifact_max_files_per_execution:
            raise ValueError("Artifact count limit exceeded")
        remaining = self.settings.artifact_max_execution_bytes - sum(
            p.stat().st_size for p in existing
        )
        limit = min(self.settings.artifact_max_bytes, remaining)
        suffix = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ""
        artifact_id = "file-" + uuid4().hex + suffix
        path = stage / artifact_id
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("xb") as output:
                while True:
                    chunk = data.read(min(65536, max(1, limit - size + 1)))
                    if not isinstance(chunk, bytes):
                        raise TypeError("Artifact streams must return bytes")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("Artifact byte limit exceeded")
                    digest.update(chunk)
                    output.write(chunk)
            descriptor = {
                "id": artifact_id,
                "kind": "file",
                "filename": filename,
                "mime_type": mime,
                "size_bytes": size,
                "sha256": digest.hexdigest(),
                "uri": f"wotbot://artifacts/{artifact_id}",
                "content_uri": f"wotbot://artifacts/{artifact_id}/content",
                "source": source or {"representation": "derived"},
            }
            (stage / f".manifest-{artifact_id}").write_text(
                json.dumps({"version": 1, **descriptor}), encoding="utf-8"
            )
            return descriptor
        except BaseException:
            path.unlink(missing_ok=True)
            (stage / f".manifest-{artifact_id}").unlink(missing_ok=True)
            raise

    def publish(self, execution_id: str, files: list[dict]) -> list[dict]:
        stage = self.stage_dir(execution_id)
        published = []
        try:
            for item in files:
                artifact_id = item["id"]
                if not valid_id(artifact_id):
                    raise ValueError("Invalid artifact id")
                data = stage / artifact_id
                manifest = stage / f".manifest-{artifact_id}"
                if data.is_symlink() or manifest.is_symlink():
                    raise ValueError("Artifact files cannot be symlinks")
                # Retention begins when the result becomes available, even if
                # the program created this file early in a long execution.
                os.utime(data, None)
                os.replace(data, self.root / ".files" / artifact_id)
                published.append(artifact_id)
                os.replace(manifest, self.root / ".manifests" / artifact_id)
            return [self.describe(item["id"]) for item in files]
        except BaseException:
            for artifact_id in published:
                (self.root / ".files" / artifact_id).unlink(missing_ok=True)
                (self.root / ".manifests" / artifact_id).unlink(missing_ok=True)
            raise

    def discard(self, execution_id: str) -> None:
        shutil.rmtree(self.stage_dir(execution_id), ignore_errors=True)

    def path(self, artifact_id: str) -> Path:
        if not valid_id(artifact_id):
            raise ValueError("Invalid artifact id")
        manifest = self.root / ".manifests" / artifact_id
        if manifest.is_symlink():
            raise ValueError("Invalid artifact manifest")
        path = (
            self.root / ".files" / artifact_id
            if manifest.is_file()
            else self.root / artifact_id
        )
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("Artifact not found")
        if manifest.is_file() and time.time() >= (
            path.stat().st_mtime + self.settings.file_artifacts_ttl_seconds
        ):
            raise FileNotFoundError("Artifact expired")
        return path

    def describe(self, artifact_id: str) -> dict[str, Any]:
        path = self.path(artifact_id)
        manifest = self.root / ".manifests" / artifact_id
        if manifest.is_file():
            descriptor = json.loads(manifest.read_text(encoding="utf-8"))
            descriptor.pop("version", None)
        else:
            # Legacy charts, images and uploads remain readable without migration.
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            descriptor = {
                "id": artifact_id,
                "filename": artifact_id,
                "kind": "plotly"
                if path.suffix == ".json"
                else (
                    "image"
                    if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                    else "file"
                ),
                "mime_type": mimetypes.guess_type(artifact_id)[0]
                or "application/octet-stream",
                "size_bytes": path.stat().st_size,
                "sha256": digest,
                "uri": f"wotbot://artifacts/{artifact_id}",
                "content_uri": f"wotbot://artifacts/{artifact_id}/content",
            }
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        ttl = (
            self.settings.file_artifacts_ttl_seconds
            if manifest.is_file()
            else self.settings.artifacts_ttl_seconds
        )
        return {
            **descriptor,
            "retention": "ephemeral",
            "ttl_seconds": ttl,
            "modified_at": modified.isoformat(),
            "expires_at": (modified + timedelta(seconds=ttl)).isoformat(),
        }

    def cleanup(self) -> None:
        cutoff = time.time() - self.settings.file_artifacts_ttl_seconds
        for path in (self.root / ".files").iterdir():
            if (
                not path.is_symlink()
                and path.is_file()
                and path.stat().st_mtime <= cutoff
            ):
                path.unlink(missing_ok=True)
                (self.root / ".manifests" / path.name).unlink(missing_ok=True)
        for path in (self.root / ".manifests").iterdir():
            if not (self.root / ".files" / path.name).is_file():
                path.unlink(missing_ok=True)
        # Allow a live execution to exceed a short configured artifact TTL.
        deadline = time.time() - max(
            self.settings.artifacts_ttl_seconds,
            self.settings.execution_timeout_seconds + 60,
        )
        for path in (self.root / ".staging").iterdir():
            if (
                path.is_dir()
                and not path.is_symlink()
                and path.stat().st_mtime < deadline
            ):
                shutil.rmtree(path, ignore_errors=True)
