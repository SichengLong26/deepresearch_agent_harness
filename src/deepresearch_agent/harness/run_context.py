"""Serializable state required to execute or resume one research Run."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .budgets import BudgetLimits, BudgetUsage
from .contracts import RunStatus, SourceMode, WorkflowMode


class RunContext(BaseModel):
    run_id: str
    session_id: str
    trigger_message_id: str
    source_mode: SourceMode
    workflow_mode: WorkflowMode
    status: RunStatus
    original_query: str
    resolved_query: Optional[str] = None
    plan_version: int = 0
    checkpoint_version: int = 0
    resume_cursor: Optional[RunStatus] = None
    charged_record_tokens: Dict[str, int] = Field(default_factory=dict)
    execution_owner: Optional[str] = None
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    model_snapshot: Dict[str, Any] = Field(default_factory=dict)
    budget_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    cancellation_requested: bool = False
    # Set only while loading an interrupted Run. It records the first stage that
    # is safe to execute after the latest durable checkpoint.
    resume_from_status: Optional[RunStatus] = None
    context_snapshot: Dict[str, Any] = Field(default_factory=dict)
    used_message_ids: list[str] = Field(default_factory=list)
    model_input: Optional[str] = None
    workflow_state: Dict[str, Any] = Field(default_factory=dict)
    report: Optional[str] = None
