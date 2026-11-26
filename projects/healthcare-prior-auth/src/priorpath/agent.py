"""PriorPath: prior authorization review agent (domain adapter + wiring).

PHI handling (see docs/SECURITY.md): the clinical note is redacted *before* it is spotlighted
into the prompt, and only minimum-necessary structured fields are rendered (age, not DOB; no
name, MRN or member id). Live mode must run on Bedrock in-account under the AWS BAA.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from agentkit import (
    AuditLog,
    BM25Index,
    CallContext,
    RoutingRule,
    ToolGateway,
    build_decision_model,
    chunk_markdown,
)
from agentkit.decision_graph import CaseRunner, GuardOutcome, Proposal, build_decision_graph
from pydantic import Field

from .policies import AGENT, MD, NURSE, build_policy
from .synthetic import build_world
from .tools import Store, build_tools

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"

SECTION_QUERY = {
    "MRI_LUMBAR": "lumbar spine MRI 72148 conservative therapy red flags",
    "TKA": "total knee arthroplasty 27447 Kellgren-Lawrence BMI",
    "GLP1": "GLP-1 weight management BMI comorbidity lifestyle program contraindication",
    "PSG": "attended polysomnography 95810 home sleep apnea test",
}


class PaProposal(Proposal):
    outcome: Literal["APPROVE", "PEND_INFO", "DENY"]
    criteria_assessment: dict[str, Literal["met", "not_met", "unknown"]] = Field(default_factory=dict)
    missing_documentation: list[str] = Field(default_factory=list)


SYSTEM = """You are PriorPath, a utilization management review assistant for Cascadia Health Plan (synthetic).
Apply ONLY the plan criteria in <evidence> to the request. The structured criteria check marks some criteria
`unknown`; read the clinical note to resolve them, quoting the phrase you relied on in the rationale.
Outcomes: APPROVE if criteria are met; PEND_INFO if documentation needed to apply criteria is missing;
DENY if documentation shows criteria are not met (a Medical Director will review every DENY).
Never infer clinical facts that are not written. Cite the criteria section ids exactly as given."""


def heuristic(ctx: dict[str, Any]) -> dict[str, Any]:
    """Structured-data-only baseline: never reads the note (where P05/P22-type facts live)."""
    f = ctx["facts"]
    crit = f["criteria"]
    if not f["eligibility"]["coverage_active"]:
        out, conf = "DENY", 0.95
    else:
        out, conf = {"met": ("APPROVE", 0.9), "not_met": ("DENY", 0.85),
                     "unknown": ("PEND_INFO", 0.85)}[crit["overall"]]
    missing = [k for k, v in crit["criteria"].items() if v == "unknown"] if out == "PEND_INFO" else []
    return {"outcome": out, "confidence": conf, "citations": ctx["evidence"]["citations"][:2],
            "criteria_assessment": crit["criteria"], "missing_documentation": missing,
            "rationale": f"Structured criteria check for {f['request']['service']}: {json.dumps(crit['criteria'])}.",
            "injection_observed": False}


class PaAdapter:
    name = "priorpath"
    proposal_schema = PaProposal
    system_prompt = SYSTEM
    agent_role = AGENT
    reviewer_roles = {"APPROVE": {NURSE, MD}, "PEND_INFO": {NURSE, MD}, "DENY": {MD}}
    auto_outcomes = {"APPROVE", "PEND_INFO"}
    transient_keys = {"note"}
    min_auto_confidence = 0.85

    def load(self, case_id: str, gw: ToolGateway, ctx: CallContext) -> dict[str, Any]:
        req = gw.call("get_request", {"request_id": case_id}, ctx)
        elig = gw.call("get_eligibility", {"member_id": req["member_id"]}, ctx)
        rec = gw.call("get_clinical_record", {"request_id": case_id}, ctx)
        crit = gw.call("check_criteria", {"service": req["service"], "findings": rec["findings"]}, ctx)
        return {"request": {k: req[k] for k in ("request_id", "service", "code", "urgent", "date_of_service")},
                "eligibility": {"coverage_active": elig["coverage_active"], "age": elig["age"]},
                "findings": rec["findings"], "criteria": crit, "note": rec["note"],
                "policy_attrs": {"coverage_active": elig["coverage_active"], "urgent": req["urgent"]}}

    def untrusted_text(self, facts: dict[str, Any]) -> str:
        return facts["note"]

    def queries(self, facts: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        q = [SECTION_QUERY[facts["request"]["service"]], "determination outcomes medical director",
             "clinical documentation minimum necessary"]
        if not facts["eligibility"]["coverage_active"]:
            q.append("eligibility coverage inactive administrative")
        if facts["request"]["urgent"]:
            q.append("expedited urgent requests 72 hours")
        return q, {}

    def render(self, facts: dict[str, Any], evidence: str, untrusted_block: str) -> str:
        case = {"request": facts["request"], "eligibility": facts["eligibility"], "findings": facts["findings"],
                "criteria_check": facts["criteria"]}
        return f"<case>\n{json.dumps(case, indent=1)}\n</case>\n<evidence>\n{evidence}\n</evidence>\n{untrusted_block}"

    def hard_rules(self, facts: dict[str, Any], proposal: dict[str, Any]) -> GuardOutcome:
        reasons, forced = [], None
        if not facts["eligibility"]["coverage_active"]:
            reasons.append("coverage inactive: administrative DENY requires authorized sign-off")
            forced = "DENY"
        if facts["request"]["urgent"] and proposal["outcome"] == "PEND_INFO":
            reasons.append("expedited request cannot be pended without clinician review")
        if proposal["outcome"] == "APPROVE" and facts["criteria"]["overall"] == "not_met":
            reasons.append("model APPROVE contradicts structured criteria check (not_met)")
        return GuardOutcome(require_human=bool(reasons), reasons=reasons, forced_outcome=forced)

    def commit(self, gw: ToolGateway, ctx: CallContext, case_id: str, final: dict[str, Any],
               idempotency_key: str) -> dict[str, Any]:
        return gw.call("record_decision", {"request_id": case_id, "outcome": final["outcome"],
                                           "rationale": final["rationale"], "citations": final["citations"],
                                           "decided_by": final["decided_by"]}, ctx, idempotency_key=idempotency_key)


def build_retriever() -> BM25Index:
    chunks = []
    for p in sorted(KNOWLEDGE.glob("*.md")):
        chunks += chunk_markdown(p.stem, p.read_text())
    return BM25Index(chunks)


def build_runner(world: dict[str, Any] | None = None, checkpointer: Any = None) -> tuple[CaseRunner, ToolGateway, Store]:
    from langgraph.checkpoint.memory import InMemorySaver

    if os.getenv("AGENT_LLM_MODE") == "live" and not os.getenv("AWS_REGION") \
            and os.getenv("PHI_FIRST_PARTY_BAA") != "true":
        raise RuntimeError("PHI workload: live mode requires Bedrock in-account (set AWS_REGION) or a "
                           "signed BAA with the model provider (PHI_FIRST_PARTY_BAA=true)")
    store = Store(world or build_world())
    gw = ToolGateway(build_policy(), AuditLog()).register(*build_tools(store))
    # PHI workload: frontier model for judgement, but only on Bedrock in-account (see ADR-0003).
    rule = RoutingRule(task="prior_auth", model="claude-opus-5-5", effort="medium")
    graph = build_decision_graph(PaAdapter(), gw, build_retriever(),
                                 model_factory=lambda: build_decision_model(rule, heuristic),
                                 checkpointer=checkpointer or InMemorySaver())
    return CaseRunner(graph, PaAdapter.reviewer_roles), gw, store
