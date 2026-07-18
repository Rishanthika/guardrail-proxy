"""
Pydantic v2 schemas shared across the proxy gateway, policy engine, and
database layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Action = Literal["block", "require_hitl", "log_and_allow"]
HITLDecision = Literal["approve", "reject"]
HITLStatus = Literal["PENDING", "APPROVED", "REJECTED"]


# -----------------------------------------------------------------------------
# Inbound tool-call request from the agent / LLM integration layer
# -----------------------------------------------------------------------------
class ToolCallRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, examples=["agent-finance-01"])
    tool_name: str = Field(..., min_length=1, examples=["database_delete"])
    parameters: dict[str, Any] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Policy engine output
# -----------------------------------------------------------------------------
class EvaluationResult(BaseModel):
    tool_name: str
    action: Action
    rule_matched: str
    reason: str
    parameter_checked: Optional[str] = None
    parameter_value: Optional[Any] = None


# -----------------------------------------------------------------------------
# HITL review webhook
# -----------------------------------------------------------------------------
class HITLReviewRequest(BaseModel):
    request_id: str
    decision: HITLDecision
    reviewer: Optional[str] = Field(
        default=None, description="Optional identifier of the human reviewer."
    )
    notes: Optional[str] = None


class HITLRequestOut(BaseModel):
    request_id: str
    agent_id: str
    tool_name: str
    parameters: dict[str, Any]
    status: HITLStatus
    created_at: datetime
    decided_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# -----------------------------------------------------------------------------
# API responses
# -----------------------------------------------------------------------------
class ToolExecutionResponse(BaseModel):
    request_id: str
    action: Action
    outcome: str
    dry_run: bool
    rule_matched: str
    reason: str
    result: Optional[dict[str, Any]] = None


class ErrorResponse(BaseModel):
    request_id: Optional[str] = None
    error: str
    detail: str


# -----------------------------------------------------------------------------
# Audit log (API-facing view of the database row)
# -----------------------------------------------------------------------------
class AuditLogOut(BaseModel):
    id: int
    request_id: str
    timestamp: datetime
    agent_id: str
    tool_name: str
    parameters: dict[str, Any]
    rule_matched: str
    action: Action
    outcome: str
    dry_run: bool

    model_config = {"from_attributes": True}
