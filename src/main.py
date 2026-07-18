"""
FastAPI Proxy Gateway — the intercepting front door every agent tool call
must pass through before it reaches a real system.

Endpoints
---------
POST /v1/execute-tool     Evaluate + (maybe) execute a proposed tool call.
POST /v1/hitl/review      Admin webhook to approve/reject a pending action.
GET  /v1/hitl/pending     List HITL requests awaiting review.
GET  /v1/audit-logs       Recent audit trail (for demos / debugging).
GET  /healthz             Liveness/readiness probe for load balancers.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from src.database import (
    create_hitl_request,
    get_hitl_request,
    get_session,
    init_db,
    list_audit_logs,
    list_hitl_requests,
    update_audit_outcome,
    write_audit_log,
)
from src.models import (
    ErrorResponse,
    HITLReviewRequest,
    ToolCallRequest,
    ToolExecutionResponse,
)
from src.policy_engine import PolicyEngine
from src.tools import ToolExecutionError, execute_tool

# -----------------------------------------------------------------------------
# Structured JSON logging
# -----------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


logger = logging.getLogger("guardrail")
logger.setLevel(settings.log_level)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logger.handlers = [_handler]
logger.propagate = False


def log_event(level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"extra_fields": fields})


# -----------------------------------------------------------------------------
# App lifecycle
# -----------------------------------------------------------------------------
policy_engine = PolicyEngine(
    policy_path=settings.policy_path, dry_run_override=settings.dry_run_override
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log_event(logging.INFO, "startup_complete", dry_run=policy_engine.dry_run)
    yield
    log_event(logging.INFO, "shutdown")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Pre-execution action guardrail proxy for autonomous agents.",
    lifespan=lifespan,
)


# -----------------------------------------------------------------------------
# Global exception handler — never leak a raw traceback to the caller
# -----------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event(
        logging.ERROR,
        "unhandled_exception",
        path=str(request.url.path),
        error=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="internal_server_error",
            detail="An unexpected error occurred. It has been logged for investigation.",
        ).model_dump(),
    )


# -----------------------------------------------------------------------------
# Health check
# -----------------------------------------------------------------------------
@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Core interceptor
# -----------------------------------------------------------------------------
@app.post("/v1/execute-tool", response_model=ToolExecutionResponse, tags=["gateway"])
async def execute_tool_endpoint(
    payload: ToolCallRequest, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    request_id = str(uuid.uuid4())
    dry_run = policy_engine.dry_run

    evaluation = await policy_engine.evaluate_action(
        agent_id=payload.agent_id,
        tool_name=payload.tool_name,
        parameters=payload.parameters,
    )

    log_event(
        logging.INFO,
        "policy_evaluated",
        request_id=request_id,
        agent_id=payload.agent_id,
        tool_name=payload.tool_name,
        action=evaluation.action,
        rule_matched=evaluation.rule_matched,
        dry_run=dry_run,
    )

    # ---- BLOCK -------------------------------------------------------
    if evaluation.action == "block":
        await write_audit_log(
            session,
            request_id=request_id,
            agent_id=payload.agent_id,
            tool_name=payload.tool_name,
            parameters=payload.parameters,
            rule_matched=evaluation.rule_matched,
            action=evaluation.action,
            outcome="blocked",
            dry_run=dry_run,
        )
        body = ToolExecutionResponse(
            request_id=request_id,
            action="block",
            outcome="blocked",
            dry_run=dry_run,
            rule_matched=evaluation.rule_matched,
            reason=evaluation.reason,
        )
        return JSONResponse(status_code=403, content=body.model_dump())

    # ---- REQUIRE HITL --------------------------------------------------
    if evaluation.action == "require_hitl":
        await create_hitl_request(
            session,
            request_id=request_id,
            agent_id=payload.agent_id,
            tool_name=payload.tool_name,
            parameters=payload.parameters,
        )
        await write_audit_log(
            session,
            request_id=request_id,
            agent_id=payload.agent_id,
            tool_name=payload.tool_name,
            parameters=payload.parameters,
            rule_matched=evaluation.rule_matched,
            action=evaluation.action,
            outcome="pending_hitl",
            dry_run=dry_run,
        )
        body = ToolExecutionResponse(
            request_id=request_id,
            action="require_hitl",
            outcome="pending_hitl",
            dry_run=dry_run,
            rule_matched=evaluation.rule_matched,
            reason=evaluation.reason,
        )
        return JSONResponse(status_code=202, content=body.model_dump())

    # ---- LOG AND ALLOW ---------------------------------------------------
    await write_audit_log(
        session,
        request_id=request_id,
        agent_id=payload.agent_id,
        tool_name=payload.tool_name,
        parameters=payload.parameters,
        rule_matched=evaluation.rule_matched,
        action=evaluation.action,
        outcome="executing",
        dry_run=dry_run,
    )
    try:
        result = await execute_tool(payload.tool_name, payload.parameters, dry_run=dry_run)
    except ToolExecutionError as exc:
        await update_audit_outcome(session, request_id=request_id, outcome="execution_failed")
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                request_id=request_id, error="tool_execution_error", detail=str(exc)
            ).model_dump(),
        )

    final_outcome = "simulated" if dry_run else "executed"
    await update_audit_outcome(session, request_id=request_id, outcome=final_outcome)

    body = ToolExecutionResponse(
        request_id=request_id,
        action="log_and_allow",
        outcome=final_outcome,
        dry_run=dry_run,
        rule_matched=evaluation.rule_matched,
        reason=evaluation.reason,
        result=result,
    )
    return JSONResponse(status_code=200, content=body.model_dump())


# -----------------------------------------------------------------------------
# HITL review webhook
# -----------------------------------------------------------------------------
@app.post("/v1/hitl/review", tags=["gateway"])
async def hitl_review(
    payload: HITLReviewRequest, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    dry_run = policy_engine.dry_run
    pending = await get_hitl_request(session, payload.request_id)

    if pending is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=payload.request_id,
                error="not_found",
                detail="No pending HITL request with that request_id.",
            ).model_dump(),
        )

    if pending.status != "PENDING":
        return JSONResponse(
            status_code=409,
            content=ErrorResponse(
                request_id=payload.request_id,
                error="already_decided",
                detail=f"This request was already {pending.status.lower()}.",
            ).model_dump(),
        )

    from datetime import datetime, timezone

    if payload.decision == "reject":
        pending.status = "REJECTED"
        pending.decided_at = datetime.now(timezone.utc)
        pending.reviewer = payload.reviewer
        pending.notes = payload.notes
        await session.commit()
        await update_audit_outcome(session, request_id=payload.request_id, outcome="rejected")
        log_event(
            logging.INFO,
            "hitl_rejected",
            request_id=payload.request_id,
            reviewer=payload.reviewer,
        )
        return JSONResponse(
            status_code=200,
            content={
                "request_id": payload.request_id,
                "status": "REJECTED",
                "detail": "Action rejected by reviewer; tool call was not executed.",
            },
        )

    # approve
    import json as _json

    parameters = _json.loads(pending.parameters_json)
    try:
        result = await execute_tool(pending.tool_name, parameters, dry_run=dry_run)
    except ToolExecutionError as exc:
        await update_audit_outcome(
            session, request_id=payload.request_id, outcome="execution_failed"
        )
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                request_id=payload.request_id, error="tool_execution_error", detail=str(exc)
            ).model_dump(),
        )

    pending.status = "APPROVED"
    pending.decided_at = datetime.now(timezone.utc)
    pending.reviewer = payload.reviewer
    pending.notes = payload.notes
    await session.commit()

    final_outcome = "approved_simulated" if dry_run else "approved_executed"
    await update_audit_outcome(session, request_id=payload.request_id, outcome=final_outcome)

    log_event(
        logging.INFO,
        "hitl_approved",
        request_id=payload.request_id,
        reviewer=payload.reviewer,
        dry_run=dry_run,
    )

    return JSONResponse(
        status_code=200,
        content={
            "request_id": payload.request_id,
            "status": "APPROVED",
            "dry_run": dry_run,
            "result": result,
        },
    )


# -----------------------------------------------------------------------------
# Convenience read endpoints (demo / operator visibility)
# -----------------------------------------------------------------------------
@app.get("/v1/hitl/pending", tags=["ops"])
async def hitl_pending(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await list_hitl_requests(session, status="PENDING")
    return [r.as_dict() for r in rows]


@app.get("/v1/audit-logs", tags=["ops"])
async def audit_logs(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    rows = await list_audit_logs(session, limit=limit)
    return [r.as_dict() for r in rows]
