from typing import Any

from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    session_id: str
    code: str


class WotCall(BaseModel):
    type: str
    thing_id: str
    name: str
    ok: bool
    input: Any | None = None
    value: Any | None = None
    uri_variables: dict[str, Any] | None = None


class StoredRecord(BaseModel):
    data: dict[str, Any]
    raw_input: str | None = None
    confidence: float | None = None


class ExecuteResponse(BaseModel):
    ok: bool
    error: str | None = None
    stdout: str
    images: list[str]
    plotly: list[str]
    files: list[dict[str, Any]] = Field(default_factory=list)
    wot_calls: list[WotCall] = Field(default_factory=list)
    records: list[StoredRecord] = Field(default_factory=list)
    reports: list[str] = Field(default_factory=list)


class WebArtifactRequest(BaseModel):
    html: str


class WebArtifactResponse(BaseModel):
    filename: str


class UploadResponse(BaseModel):
    filename: str
    size_bytes: int
