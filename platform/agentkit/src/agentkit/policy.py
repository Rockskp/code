"""A small policy engine with Cedar semantics: default-deny, explicit forbid overrides permit.

In production the same rules are written in Cedar and enforced by AgentCore Policy at the
Gateway (see each project's `policies/*.cedar`). This in-process engine enforces them *again*
inside the app (defence in depth) and makes the rules unit-testable offline.

Modes
- ENFORCE  : deny means the action does not run.
- LOG_ONLY : deny is recorded but the action runs. Use only when rolling a new rule out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Effect(StrEnum):
    PERMIT = "permit"
    FORBID = "forbid"


class Mode(StrEnum):
    ENFORCE = "enforce"
    LOG_ONLY = "log_only"


Condition = Callable[[Mapping[str, Any]], bool]


def _always(_ctx: Mapping[str, Any]) -> bool:
    return True


@dataclass(frozen=True)
class Rule:
    id: str
    effect: Effect
    actions: frozenset[str]
    principals: frozenset[str] = frozenset({"*"})
    when: Condition = _always
    reason: str = ""

    def matches(self, principal_role: str, action: str, ctx: Mapping[str, Any]) -> bool:
        if action not in self.actions and "*" not in self.actions:
            return False
        if "*" not in self.principals and principal_role not in self.principals:
            return False
        try:
            return bool(self.when(ctx))
        except (KeyError, TypeError, ValueError):
            # A condition that cannot be evaluated is treated as matching for FORBID rules
            # (fail closed) and non-matching for PERMIT rules.
            return self.effect is Effect.FORBID


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    enforced: bool
    matched: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def would_deny(self) -> bool:
        return not self.allowed


@dataclass
class PolicyEngine:
    rules: list[Rule] = field(default_factory=list)
    mode: Mode = Mode.ENFORCE

    def add(self, *rules: Rule) -> PolicyEngine:
        self.rules.extend(rules)
        return self

    def evaluate(self, principal_role: str, action: str, ctx: Mapping[str, Any]) -> PolicyDecision:
        permits = [r for r in self.rules if r.effect is Effect.PERMIT and r.matches(principal_role, action, ctx)]
        forbids = [r for r in self.rules if r.effect is Effect.FORBID and r.matches(principal_role, action, ctx)]
        allowed = bool(permits) and not forbids
        matched = tuple(r.id for r in (forbids or permits))
        reasons = tuple(r.reason for r in forbids) if forbids else (() if permits else ("no permit (default deny)",))
        if self.mode is Mode.LOG_ONLY:
            return PolicyDecision(True, False, matched, reasons if not allowed else ())
        return PolicyDecision(allowed, True, matched, reasons)


def permit(rule_id: str, actions: set[str], principals: set[str] | None = None,
           when: Condition | None = None, reason: str = "") -> Rule:
    return Rule(rule_id, Effect.PERMIT, frozenset(actions), frozenset(principals or {"*"}),
                when or _always, reason)


def forbid(rule_id: str, actions: set[str], principals: set[str] | None = None,
           when: Condition | None = None, reason: str = "") -> Rule:
    return Rule(rule_id, Effect.FORBID, frozenset(actions), frozenset(principals or {"*"}),
                when or _always, reason)
