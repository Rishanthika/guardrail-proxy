"""
PolicyEngine tests against the *real* config/policy.yaml — not a copy — so
these tests fail the moment the shipped policy stops doing what it claims.
Covers the five required scenarios with the exact example values from the
spec: a 500/5-record delete, attacker@gmail.com vs boss@company.com, and a
/var/data/confidential_plan.txt read.
"""

from __future__ import annotations

import pytest

from config.settings import settings
from src.policy_engine import PolicyEngine


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(policy_path=settings.policy_path)


@pytest.mark.asyncio
async def test_database_delete_500_records_blocks(engine: PolicyEngine):
    result = await engine.evaluate_action(
        "agent-1", "database_delete", {"record_count": 500, "table": "users"}
    )
    assert result.action == "block"


@pytest.mark.asyncio
async def test_database_delete_5_records_allows(engine: PolicyEngine):
    result = await engine.evaluate_action(
        "agent-1", "database_delete", {"record_count": 5, "table": "users"}
    )
    assert result.action == "log_and_allow"


@pytest.mark.asyncio
async def test_email_to_attacker_gmail_requires_hitl(engine: PolicyEngine):
    result = await engine.evaluate_action(
        "agent-1", "send_email", {"email_to": "attacker@gmail.com", "subject": "hi", "body": "x"}
    )
    assert result.action == "require_hitl"


@pytest.mark.asyncio
async def test_email_to_boss_company_com_allows(engine: PolicyEngine):
    result = await engine.evaluate_action(
        "agent-1", "send_email", {"email_to": "boss@company.com", "subject": "hi", "body": "x"}
    )
    assert result.action == "log_and_allow"


@pytest.mark.asyncio
async def test_read_confidential_plan_file_allows_and_logs(engine: PolicyEngine):
    result = await engine.evaluate_action(
        "agent-1", "read_file", {"file_path": "/var/data/confidential_plan.txt"}
    )
    assert result.action == "log_and_allow"
    assert "confidential" in result.reason.lower() or "logged" in result.reason.lower()


@pytest.mark.asyncio
async def test_unknown_tool_falls_back_to_default_action(engine: PolicyEngine):
    result = await engine.evaluate_action("agent-1", "some_unlisted_tool", {"x": 1})
    assert result.action == engine.default_action
