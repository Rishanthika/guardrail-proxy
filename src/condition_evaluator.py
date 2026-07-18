"""
Safe Condition Evaluator.

Lets `config/policy.yaml` express rules as readable boolean expressions —

    parameters.record_count > 100
    not parameters.email_to.endswith('@company.com')
    'confidential' in parameters.file_path

— without ever calling eval()/exec() on the string. That distinction matters
a lot here: this file sits directly in the path of a security control, and
policy.yaml is exactly the kind of config a future teammate (or, in a worst
case, an attacker who manages to write to config) might be able to edit.
Raw eval() on that string would hand them arbitrary code execution inside
the gateway. This evaluator instead parses the expression into an AST and
walks it with an explicit whitelist:

  - The only identifier in scope is `parameters` (the tool call's own
    parameters dict) — no builtins, no imports, no other names.
  - Attribute access on `parameters` (e.g. `.record_count`) is a dict
    `.get()`, not real Python attribute resolution — `parameters.__class__`
    safely resolves to `None`, it does not leak the dict's type.
  - Only a small whitelist of harmless string methods may be called
    (`endswith`, `startswith`, `lower`, `upper`, `strip`, `lstrip`,
    `rstrip`), and only on string values.
  - Comparisons (`>`, `>=`, `<`, `<=`, `==`, `!=`, `in`, `not in`), boolean
    combinators (`and`, `or`, `not`), and literal constants are the only
    other constructs allowed.

Anything outside that whitelist — arbitrary function calls, imports,
attribute chains onto non-`parameters` objects, comprehensions, lambdas,
subscripts, etc. — raises `UnsafeConditionError` immediately rather than
being evaluated.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

_COMPARE_OPS: dict[type, Any] = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_ALLOWED_STRING_METHODS = {
    "endswith",
    "startswith",
    "lower",
    "upper",
    "strip",
    "lstrip",
    "rstrip",
}


class UnsafeConditionError(ValueError):
    """Raised when a policy.yaml condition uses syntax outside the whitelist."""


class SafeConditionEvaluator:
    """Evaluates one whitelisted boolean expression against a `parameters` dict."""

    def evaluate(self, condition: str, parameters: dict[str, Any]) -> bool:
        try:
            tree = ast.parse(condition, mode="eval")
        except SyntaxError as exc:
            raise UnsafeConditionError(
                f"Could not parse condition {condition!r}: {exc}"
            ) from exc
        return bool(self._eval(tree.body, parameters))

    # ------------------------------------------------------------------
    def _eval(self, node: ast.AST, parameters: dict[str, Any]) -> Any:
        if isinstance(node, ast.Expression):
            return self._eval(node.body, parameters)

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Name):
            if node.id != "parameters":
                raise UnsafeConditionError(
                    f"Unknown identifier '{node.id}'; only 'parameters' is in scope"
                )
            return parameters

        if isinstance(node, ast.Attribute):
            base = self._eval(node.value, parameters)
            if not isinstance(base, dict):
                raise UnsafeConditionError(
                    "Attribute access is only allowed on 'parameters' fields"
                )
            # dict.get(), NOT real attribute resolution — parameters.__class__
            # safely returns None instead of leaking the dict type.
            return base.get(node.attr)

        if isinstance(node, ast.Compare):
            left = self._eval(node.left, parameters)
            result = True
            for op, comparator in zip(node.ops, node.comparators):
                op_type = type(op)
                if op_type not in _COMPARE_OPS:
                    raise UnsafeConditionError(
                        f"Comparison operator '{op_type.__name__}' is not allowed"
                    )
                right = self._eval(comparator, parameters)
                try:
                    result = result and _COMPARE_OPS[op_type](left, right)
                except TypeError:
                    # e.g. comparing None > 100 because the parameter was
                    # never supplied — treat as "condition does not match"
                    # rather than raising, since a missing parameter is a
                    # policy-relevant fact, not a program error.
                    result = False
                left = right
            return result

        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(self._eval(v, parameters)) for v in node.values)
            if isinstance(node.op, ast.Or):
                return any(bool(self._eval(v, parameters)) for v in node.values)
            raise UnsafeConditionError("Unsupported boolean operator")

        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return not self._eval(node.operand, parameters)
            raise UnsafeConditionError("Only the 'not' unary operator is allowed")

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                raise UnsafeConditionError(
                    "Only whitelisted string method calls are allowed, e.g. "
                    "parameters.email_to.endswith('@company.com')"
                )
            method_name = node.func.attr
            if method_name not in _ALLOWED_STRING_METHODS:
                raise UnsafeConditionError(f"Method '{method_name}' is not allowed")
            if node.keywords:
                raise UnsafeConditionError("Keyword arguments are not allowed")
            target = self._eval(node.func.value, parameters)
            if not isinstance(target, str):
                raise UnsafeConditionError(
                    "String methods can only be called on string parameter values"
                )
            args = [self._eval(a, parameters) for a in node.args]
            return getattr(target, method_name)(*args)

        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(el, parameters) for el in node.elts]

        raise UnsafeConditionError(f"Unsupported syntax in condition: {type(node).__name__}")
