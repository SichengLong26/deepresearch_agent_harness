"""HTTP DTOs for the phase-4 FastAPI boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(ApiModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody
    request_id: str


class SessionPatch(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "archived"] | None = None


class MessageSend(ApiModel):
    target_run_id: str | None = None
    new_run: bool = False
    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=20000)
    source_mode: SourceMode
    workflow_mode: WorkflowMode = WorkflowMode.DEEP_RESEARCH
    report_type: Literal["brief", "long_document"] = "brief"


class ClarificationSubmit(ApiModel):
    content: str = Field(min_length=1, max_length=10000)


class RunAccepted(ApiModel):
    command_id: str | None = None
    message_id: str
    run_id: str
    status: str
    events_url: str
    created: bool


class RunControl(ApiModel):
    run_id: str
    status: str


class MemoryLifecycleUpdate(ApiModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    status: Literal["candidate", "active", "rejected", "archived", "expired"] | None = None
    expires_at: str | None = Field(default=None, max_length=40)


class MemoryCreate(ApiModel):
    target: Literal["user", "project"]
    content: str = Field(min_length=1, max_length=10000)
    kind: Literal["preference", "fact", "decision", "lesson", "note"] = "note"
    provenance_refs: list[str] = Field(default_factory=list, max_length=20)
    activate: bool = False


class SkillActionResponse(ApiModel):
    accepted: bool
    reason: str
