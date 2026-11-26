"""In-app mirror of policies/uw.cedar (parity enforced by tests)."""

from __future__ import annotations

from agentkit import Mode, PolicyEngine, forbid, permit

READ_TOOLS = {"get_submission", "get_account_history", "appetite_check", "rate_risk"}
AGENT, UW, SENIOR = "uw_agent", "underwriter", "senior_underwriter"

RULE_IDS = {"read-all", "agent-quote-refer", "uw-any", "senior-any", "send-humans", "decline-humans-only",
            "agent-authority-limit", "uw-authority-limit"}


def build_policy(mode: Mode = Mode.ENFORCE) -> PolicyEngine:
    return PolicyEngine(mode=mode).add(
        permit("read-all", READ_TOOLS, {AGENT, UW, SENIOR}),
        permit("agent-quote-refer", {"record_decision"}, {AGENT}, when=lambda c: c["outcome"] in {"QUOTE", "REFER"}),
        permit("uw-any", {"record_decision"}, {UW}),
        permit("senior-any", {"record_decision"}, {SENIOR}),
        permit("send-humans", {"send_quote_to_broker"}, {UW, SENIOR}),
        forbid("decline-humans-only", {"record_decision"}, {AGENT}, when=lambda c: c["outcome"] == "DECLINE",
               reason="only an underwriter may decline"),
        forbid("agent-authority-limit", {"record_decision"}, {AGENT},
               when=lambda c: c["outcome"] == "QUOTE" and (c["tiv"] > 5_000_000 or (c["premium"] or 0) > 50_000),
               reason="exceeds agent delegated authority (TIV 5M / premium 50k)"),
        forbid("uw-authority-limit", {"record_decision"}, {UW},
               when=lambda c: c["outcome"] == "QUOTE" and c["tiv"] > 25_000_000,
               reason="exceeds underwriter authority; senior underwriter required"),
    )
