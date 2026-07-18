"""
Data-driven Policy Engine.

Loads config/policy.yaml once at startup and evaluates every intercepted
tool call against it. Rules are a flat, ordered list:

    rules:
      - tool: "database_delete"
        condition: "parameters.record_count > 100"
        action: "block"
        reason: "..."

For a given tool_name, the engine walks its rules in file order and returns
the action of the first rule whose `condition` evaluates true. Conditions
are evaluated by SafeConditionEvaluator (src/condition_evaluator.py) — a
whitelisted AST walker, never eval()/exec() — so policy.yaml stays purely
declarative and safe to edit without touching Python.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from src.condition_evaluator import SafeConditionEvaluator, UnsafeConditionError
from src.models import EvaluationResult


class PolicyLoadError(RuntimeError):
    """Raised when policy.yaml is missing or malformed."""


class PolicyEngine:
    def __init__(self, policy_path: Path, dry_run_override: Optional[bool] = None):
        self.policy_path = Path(policy_path)
        self._dry_run_override = dry_run_override
        self._policy: dict[str, Any] = {}
        self._evaluator = SafeConditionEvaluator()
        self.reload()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def reload(self) -> None:
        if not self.policy_path.exists():
            raise PolicyLoadError(f"Policy file not found: {self.policy_path}")
        with self.policy_path.open("r") as fh:
            data = yaml.safe_load(fh) or {}
        if "rules" not in data or not isinstance(data["rules"], list):
            raise PolicyLoadError(
                "policy.yaml must define a top-level `rules` list, e.g.\n"
                "rules:\n  - tool: \"database_delete\"\n"
                "    condition: \"parameters.record_count > 100\"\n"
                "    action: \"block\""
            )
        for i, rule in enumerate(data["rules"]):
            for required in ("tool", "condition", "action"):
                if required not in rule:
                    raise PolicyLoadError(f"rules[{i}] is missing required key '{required}'")
        self._policy = data

    @property
    def dry_run(self) -> bool:
        if self._dry_run_override is not None:
            return self._dry_run_override
        return bool(self._policy.get("dry_run", False))

    @property
    def default_action(self) -> str:
        return self._policy.get("default_action", "log_and_allow")

    @property
    def rules(self) -> list[dict[str, Any]]:
        return self._policy.get("rules", [])

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    async def evaluate_action(
        self, agent_id: str, tool_name: str, parameters: dict[str, Any]
    ) -> EvaluationResult:
        """
        Evaluate a proposed tool call against policy.yaml and return the
        matched rule, the action to take, and the human-readable reason.

        Async so callers on the FastAPI hot path can `await` it uniformly
        even though today's evaluation is pure in-memory logic; this leaves
        room to back the engine with a remote policy service later without
        changing call sites.
        """
        matching_rules = [r for r in self.rules if r["tool"] == tool_name]

        if not matching_rules:
            return EvaluationResult(
                tool_name=tool_name,
                action=self.default_action,  # type: ignore[arg-type]
                rule_matched="none (no rule defined for this tool)",
                reason=(
                    f"No policy rule exists for tool '{tool_name}'; "
                    f"falling back to default_action='{self.default_action}'."
                ),
            )

        for index, rule in enumerate(matching_rules):
            condition = rule["condition"]
            try:
                matched = self._evaluator.evaluate(condition, parameters)
            except UnsafeConditionError as exc:
                # A malformed/unsafe condition in policy.yaml is a config
                # bug, not grounds to silently allow a tool call through —
                # fail safe by treating this rule as a block.
                return EvaluationResult(
                    tool_name=tool_name,
                    action="block",
                    rule_matched=f"{tool_name}[{index}] (invalid condition)",
                    reason=f"Policy condition could not be safely evaluated: {exc}",
                    parameter_checked=condition,
                )
            if matched:
                return EvaluationResult(
                    tool_name=tool_name,
                    action=rule["action"],
                    rule_matched=f"{tool_name}[{index}]: {condition}",
                    reason=rule.get("reason", "Matched policy condition."),
                    parameter_checked=condition,
                    parameter_value=parameters,
                )

        # Every rule for this tool was evaluated and none matched.
        return EvaluationResult(
            tool_name=tool_name,
            action=self.default_action,  # type: ignore[arg-type]
            rule_matched=f"{tool_name}.default",
            reason=(
                f"No condition matched for tool '{tool_name}' with parameters "
                f"{parameters!r}; falling back to default_action='{self.default_action}'."
            ),
        )
