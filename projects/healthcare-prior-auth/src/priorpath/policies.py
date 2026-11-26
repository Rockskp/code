"""In-app mirror of policies/um.cedar (parity enforced by tests)."""

from __future__ import annotations

from agentkit import Mode, PolicyEngine, forbid, permit

READ_TOOLS = {"get_request", "get_eligibility", "get_clinical_record", "check_criteria"}
AGENT, NURSE, MD = "pa_agent", "um_nurse", "medical_director"

RULE_IDS = {"read-all", "agent-approve-pend", "nurse-approve-pend", "md-any", "deny-md-only",
            "agent-no-approve-inactive", "no-pend-urgent-unreviewed"}


def build_policy(mode: Mode = Mode.ENFORCE) -> PolicyEngine:
    return PolicyEngine(mode=mode).add(
        permit("read-all", READ_TOOLS, {AGENT, NURSE, MD}),
        permit("agent-approve-pend", {"record_decision"}, {AGENT},
               when=lambda c: c["outcome"] in {"APPROVE", "PEND_INFO"}),
        permit("nurse-approve-pend", {"record_decision"}, {NURSE},
               when=lambda c: c["outcome"] in {"APPROVE", "PEND_INFO"}),
        permit("md-any", {"record_decision"}, {MD}),
        forbid("deny-md-only", {"record_decision"}, {AGENT, NURSE}, when=lambda c: c["outcome"] == "DENY",
               reason="adverse determinations require a Medical Director"),
        forbid("agent-no-approve-inactive", {"record_decision"}, {AGENT},
               when=lambda c: c["outcome"] == "APPROVE" and not c["coverage_active"],
               reason="coverage inactive"),
        forbid("no-pend-urgent-unreviewed", {"record_decision"}, {AGENT},
               when=lambda c: c["outcome"] == "PEND_INFO" and c["urgent"],
               reason="expedited requests may not be pended without clinician review"),
    )
