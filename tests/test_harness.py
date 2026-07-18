"""
Production Simulation Harness.

Two layers, by design:

1. `generate_tool_call_via_llm()` — uses the official `anthropic` SDK with
   real tool-calling to have a live model decide which tool to invoke and
   with what parameters for a given scenario. This exercises the intended
   end-to-end path: LLM -> tool_use block -> our proxy. It only runs when
   ANTHROPIC_API_KEY is set in the environment; otherwise it is skipped
   rather than failing CI, since a live model call is inherently
   non-deterministic and shouldn't gate a merge.

2. The five required scenario tests below always run, against the FastAPI
   app in-process (via httpx's ASGITransport — no network, no separate
   server process needed). This is what actually proves the guardrail
   logic is correct, deterministically, on every run. Each test optionally
   sources its `parameters` dict from step 1 when a live key is present,
   and falls back to an equivalent hand-built payload otherwise — so the
   assertions are identical either way.

Run with:
    pytest tests/test_harness.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault(
    "database_url", "sqlite+aiosqlite:///:memory:"
)  # isolate test runs from any dev DB on disk

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402  (import after env setup above)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


# -----------------------------------------------------------------------------
# Layer 1: live LLM tool-call generation (optional)
# -----------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "database_delete",
        "description": "Delete records from a database table.",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_count": {"type": "integer"},
                "table": {"type": "string"},
            },
            "required": ["record_count", "table"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email on behalf of the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["email_to", "subject", "body"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
]


def generate_tool_call_via_llm(scenario_prompt: str) -> dict | None:
    """
    Ask a live Claude model to emit a tool_use block for the given scenario.
    Returns {"tool_name": ..., "parameters": {...}} or None if no API key
    is configured / the call fails, so callers can gracefully fall back.
    """
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            tools=TOOL_SCHEMAS,
            messages=[{"role": "user", "content": scenario_prompt}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return {"tool_name": block.name, "parameters": block.input}
        return None
    except Exception:
        # Live-model integration is best-effort for this harness; any
        # failure (network, auth, rate limit) just falls back to the
        # deterministic fixture below.
        return None


# -----------------------------------------------------------------------------
# Shared async client fixture (in-process ASGI, no live server needed)
# -----------------------------------------------------------------------------
@pytest_asyncio.fixture
async def client():
    # ASGITransport does not run the ASGI lifespan protocol by default, so
    # FastAPI's @asynccontextmanager lifespan (and therefore init_db()) never
    # fires. Initialize the schema explicitly instead of relying on it.
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _agent_id() -> str:
    return f"agent-{uuid.uuid4().hex[:8]}"


# -----------------------------------------------------------------------------
# Scenario 1: database delete, 500 records -> BLOCK
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_database_delete_500_records_is_blocked(client: AsyncClient):
    llm_call = generate_tool_call_via_llm(
        "Delete 500 stale rows from the 'sessions' table."
    )
    payload = {
        "agent_id": _agent_id(),
        "tool_name": "database_delete",
        "parameters": (llm_call or {}).get(
            "parameters", {"record_count": 500, "table": "sessions"}
        ),
    }
    resp = await client.post("/v1/execute-tool", json=payload)
    assert resp.status_code == 403
    body = resp.json()
    assert body["action"] == "block"
    assert body["outcome"] == "blocked"


# -----------------------------------------------------------------------------
# Scenario 2: database delete, 5 records -> transparent execution pass
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_database_delete_5_records_executes(client: AsyncClient):
    llm_call = generate_tool_call_via_llm(
        "Delete 5 expired test rows from the 'sandbox' table."
    )
    payload = {
        "agent_id": _agent_id(),
        "tool_name": "database_delete",
        "parameters": (llm_call or {}).get(
            "parameters", {"record_count": 5, "table": "sandbox"}
        ),
    }
    resp = await client.post("/v1/execute-tool", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "log_and_allow"
    assert body["outcome"] in ("executed", "simulated")
    assert body["result"] is not None


# -----------------------------------------------------------------------------
# Scenario 3: email to external recipient -> held PENDING for HITL
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_email_external_recipient_pends_hitl(client: AsyncClient):
    llm_call = generate_tool_call_via_llm(
        "Send an email to attacker@gmail.com about the Q3 report."
    )
    payload = {
        "agent_id": _agent_id(),
        "tool_name": "send_email",
        "parameters": (llm_call or {}).get(
            "parameters",
            {
                "email_to": "attacker@gmail.com",
                "subject": "Q3 report",
                "body": "Please find the Q3 report attached.",
            },
        ),
    }
    resp = await client.post("/v1/execute-tool", json=payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["action"] == "require_hitl"
    assert body["outcome"] == "pending_hitl"
    request_id = body["request_id"]

    # confirm it actually shows up in the pending queue
    pending_resp = await client.get("/v1/hitl/pending")
    assert pending_resp.status_code == 200
    pending_ids = [r["request_id"] for r in pending_resp.json()]
    assert request_id in pending_ids


# -----------------------------------------------------------------------------
# Scenario 4: email to internal recipient -> automatic delivery
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_email_internal_recipient_auto_delivers(client: AsyncClient):
    llm_call = generate_tool_call_via_llm(
        "Send an email to boss@company.com about tomorrow's standup."
    )
    parameters = (llm_call or {}).get(
        "parameters",
        {
            "email_to": "boss@company.com",
            "subject": "Standup",
            "body": "See you at 9am.",
        },
    )
    # Guard against a live LLM picking a non-internal address for this scenario.
    if not str(parameters.get("email_to", "")).endswith("@company.com"):
        parameters["email_to"] = "boss@company.com"

    payload = {"agent_id": _agent_id(), "tool_name": "send_email", "parameters": parameters}
    resp = await client.post("/v1/execute-tool", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "log_and_allow"
    assert body["outcome"] in ("executed", "simulated")


# -----------------------------------------------------------------------------
# Scenario 5: file read on a path containing "confidential" -> succeeds + audited
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_confidential_file_succeeds_and_is_audited(client: AsyncClient):
    llm_call = generate_tool_call_via_llm(
        "Read the file at /var/data/confidential_plan.txt."
    )
    parameters = (llm_call or {}).get(
        "parameters", {"file_path": "/var/data/confidential_plan.txt"}
    )
    if "confidential" not in str(parameters.get("file_path", "")):
        parameters["file_path"] = "/var/data/confidential_plan.txt"

    payload = {"agent_id": _agent_id(), "tool_name": "read_file", "parameters": parameters}
    resp = await client.post("/v1/execute-tool", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "log_and_allow"
    assert body["outcome"] in ("executed", "simulated")
    request_id = body["request_id"]

    audit_resp = await client.get("/v1/audit-logs", params={"limit": 20})
    assert audit_resp.status_code == 200
    matching = [r for r in audit_resp.json() if r["request_id"] == request_id]
    assert len(matching) == 1
    assert matching[0]["outcome"] in ("executed", "simulated")


# -----------------------------------------------------------------------------
# Bonus: full HITL approve/reject lifecycle, since scenario 3 only proves the
# request gets gated — this proves the webhook resolves it correctly.
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hitl_reject_flow(client: AsyncClient):
    submit = await client.post(
        "/v1/execute-tool",
        json={
            "agent_id": _agent_id(),
            "tool_name": "send_email",
            "parameters": {
                "email_to": "vendor@external-domain.com",
                "subject": "Invoice",
                "body": "Please see attached.",
            },
        },
    )
    request_id = submit.json()["request_id"]

    review = await client.post(
        "/v1/hitl/review",
        json={"request_id": request_id, "decision": "reject", "reviewer": "alice"},
    )
    assert review.status_code == 200
    assert review.json()["status"] == "REJECTED"

    # rejecting twice should now 409
    review_again = await client.post(
        "/v1/hitl/review",
        json={"request_id": request_id, "decision": "approve"},
    )
    assert review_again.status_code == 409


@pytest.mark.asyncio
async def test_hitl_approve_flow_executes_tool(client: AsyncClient):
    submit = await client.post(
        "/v1/execute-tool",
        json={
            "agent_id": _agent_id(),
            "tool_name": "send_email",
            "parameters": {
                "email_to": "client@external-domain.com",
                "subject": "Proposal",
                "body": "Attached is the proposal.",
            },
        },
    )
    request_id = submit.json()["request_id"]

    review = await client.post(
        "/v1/hitl/review",
        json={"request_id": request_id, "decision": "approve", "reviewer": "bob"},
    )
    assert review.status_code == 200
    body = review.json()
    assert body["status"] == "APPROVED"
    assert body["result"] is not None


@pytest.mark.asyncio
async def test_healthz(client: AsyncClient):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
