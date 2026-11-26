"""In-app mirror of policies/aml.cedar. Both must change together (test_policy_parity checks ids)."""

from __future__ import annotations

from agentkit import Mode, PolicyEngine, forbid, permit

READ_TOOLS = {"get_alert", "get_customer", "get_transactions", "screen_sanctions", "risk_signals"}
AGENT, ANALYST, OFFICER = "aml_agent", "aml_analyst", "bsa_officer"

RULE_IDS = {"read-all", "agent-close-escalate", "analyst-close-escalate", "officer-any",
            "agent-no-sar", "agent-no-close-high-risk", "no-close-sanctions-hit"}


def build_policy(mode: Mode = Mode.ENFORCE) -> PolicyEngine:
    return PolicyEngine(mode=mode).add(
        permit("read-all", READ_TOOLS, {AGENT, ANALYST, OFFICER}),
        permit("agent-close-escalate", {"record_decision"}, {AGENT},
               when=lambda c: c["outcome"] in {"CLOSE", "ESCALATE"}),
        permit("analyst-close-escalate", {"record_decision"}, {ANALYST},
               when=lambda c: c["outcome"] in {"CLOSE", "ESCALATE"}),
        permit("officer-any", {"record_decision"}, {OFFICER}),
        forbid("agent-no-sar", {"record_decision"}, {AGENT}, when=lambda c: c["outcome"] == "SAR",
               reason="only a BSA Officer may approve a SAR recommendation"),
        forbid("agent-no-close-high-risk", {"record_decision"}, {AGENT},
               when=lambda c: c["outcome"] == "CLOSE" and (c["customer_risk"] == "high" or c["pep"]),
               reason="agent may not close alerts on high-risk or PEP customers"),
        forbid("no-close-sanctions-hit", {"record_decision"}, {AGENT, ANALYST},
               when=lambda c: c["outcome"] == "CLOSE" and c["sanctions_score"] >= 0.85,
               reason="potential sanctions match cannot be closed below BSA Officer"),
    )
