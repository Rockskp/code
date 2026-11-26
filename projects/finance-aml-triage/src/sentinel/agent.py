"""Sentinel: L1 AML alert triage agent (domain adapter + wiring)."""

from __future__ import annotations

import json
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

from .policies import AGENT, ANALYST, OFFICER, build_policy
from .synthetic import build_world
from .tools import Store, build_tools

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"


class AmlProposal(Proposal):
    outcome: Literal["CLOSE", "ESCALATE", "SAR"]
    indicators: list[str] = Field(default_factory=list, description="Indicators that drove the outcome")


SYSTEM = """You are Sentinel, an L1 AML alert triage analyst at Northbridge Bank (synthetic).
Decide CLOSE, ESCALATE or SAR for one transaction-monitoring alert using ONLY the computed signals,
KYC profile and the manual excerpts in <evidence>. Cite the excerpt ids you rely on, exactly as given.
Never compute thresholds yourself: the signals are already computed. When indicators conflict or the
manual does not cover the case, choose the more cautious outcome and lower your confidence.
State the rule, the indicators checked, the values observed and why the outcome follows."""


def heuristic(ctx: dict[str, Any]) -> dict[str, Any]:
    """Rules-only baseline. Deliberately does not read free-text KYC notes (seasonality, source of
    funds documents), which is exactly where an LLM should add value - see docs/EVALS.md."""
    s, sanc = ctx["facts"]["signals"], ctx["facts"]["sanctions"]["score"]
    cites = ctx["evidence"]["citations"][:3]
    ind: list[str] = []
    if sanc >= 0.85:
        out, conf, ind = "SAR", 0.95, ["sanctions_potential_match"]
    elif s["structuring_deposits_14d"] >= 3:
        out, conf, ind = "SAR", 0.9, ["structuring"]
    elif s["pass_through_ratio"] >= 0.8 and s["velocity_ratio"] >= 3:
        out, conf, ind = "SAR", 0.9, ["rapid_movement", "velocity"]
    elif (s["high_risk_geo_txns"] or s["structuring_deposits_14d"] == 2 or s["pass_through_ratio"] >= 0.8
          or s["velocity_ratio"] >= 1.5 or sanc >= 0.70):
        out, conf = "ESCALATE", 0.8
        ind = [k for k, v in {"high_risk_geo": s["high_risk_geo_txns"], "structuring": s["structuring_deposits_14d"] == 2,
                              "rapid_movement": s["pass_through_ratio"] >= 0.8, "velocity": s["velocity_ratio"] >= 1.5,
                              "sanctions_name_review": sanc >= 0.70}.items() if v]
    else:
        out, conf = "CLOSE", 0.85
    return {"outcome": out, "confidence": conf, "citations": cites, "indicators": ind,
            "rationale": f"Rule-based baseline: signals={json.dumps(s)}, sanctions_score={sanc}.",
            "injection_observed": False}


class AmlAdapter:
    name = "sentinel"
    proposal_schema = AmlProposal
    system_prompt = SYSTEM
    agent_role = AGENT
    reviewer_roles = {"CLOSE": {ANALYST, OFFICER}, "ESCALATE": {ANALYST, OFFICER}, "SAR": {OFFICER}}
    auto_outcomes = {"CLOSE", "ESCALATE"}
    transient_keys = {"memo"}
    min_auto_confidence = 0.8

    def load(self, case_id: str, gw: ToolGateway, ctx: CallContext) -> dict[str, Any]:
        alert = gw.call("get_alert", {"alert_id": case_id}, ctx)
        cid = alert["customer_id"]
        cust = gw.call("get_customer", {"customer_id": cid}, ctx)
        txns = gw.call("get_transactions", {"customer_id": cid, "days": 30}, ctx)
        names = sorted({t["counterparty"] for t in txns if t["type"].startswith("wire")})
        sanctions = gw.call("screen_sanctions", {"names": names}, ctx) if names else {"score": 0.0}
        signals = gw.call("risk_signals", {"customer_id": cid}, ctx)
        return {"alert": {k: v for k, v in alert.items() if k != "memo"}, "memo": alert.get("memo", ""),
                "customer": cust, "signals": signals, "sanctions": sanctions, "txn_count": len(txns),
                "policy_attrs": {"customer_risk": cust["risk_rating"], "pep": cust["pep"],
                                 "sanctions_score": sanctions["score"]}}

    def untrusted_text(self, facts: dict[str, Any]) -> str:
        return facts.get("memo", "")

    def queries(self, facts: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        s, rule = facts["signals"], facts["alert"]["rule"]
        q = [f"{rule.replace('_', ' ').lower()} alert disposition", "documentation standard disposition"]
        if s["structuring_deposits_14d"]:
            q.append("structuring cash deposits below threshold")
        if s["pass_through_ratio"] > 0:
            q.append("rapid movement pass-through wires")
        if s["velocity_ratio"] >= 1.5:
            q.append("velocity expected monthly volume seasonality")
        if s["high_risk_geo_txns"] or s["foreign_card_unexplained"] or s["foreign_card_covered_by_travel_notice"]:
            q.append("high-risk geographies travel notice")
        if facts["sanctions"]["score"] >= 0.7:
            q.append("sanctions screening potential match similarity")
        if facts["customer"]["pep"] or facts["customer"]["risk_rating"] == "high":
            q.append("customer risk rating PEP human review")
        q.append("false positive patterns close")
        return q, {}

    def render(self, facts: dict[str, Any], evidence: str, untrusted_block: str) -> str:
        public = {k: facts[k] for k in ("alert", "signals", "sanctions")}
        kyc = {k: facts["customer"][k] for k in ("segment", "risk_rating", "pep", "expected_monthly_volume",
                                                 "occupation", "kyc_notes", "travel_notice_countries")}
        return (f"<case>\n{json.dumps(public, indent=1)}\n</case>\n<kyc>\n{json.dumps(kyc, indent=1)}\n</kyc>\n"
                f"<evidence>\n{evidence}\n</evidence>\n{untrusted_block}")

    def hard_rules(self, facts: dict[str, Any], proposal: dict[str, Any]) -> GuardOutcome:
        reasons, forced = [], None
        if facts["sanctions"]["score"] >= 0.85 and proposal["outcome"] != "SAR":
            reasons.append("sanctions score >= 0.85 forces SAR recommendation")
            forced = "SAR"
        if facts["customer"]["pep"] or facts["customer"]["risk_rating"] == "high":
            reasons.append("high-risk/PEP customer requires analyst sign-off")
        if facts["signals"]["structuring_deposits_14d"] >= 3 and proposal["outcome"] == "CLOSE":
            reasons.append("3+ structuring deposits within 14 days is a SAR recommendation per manual")
            forced = "SAR"
        return GuardOutcome(require_human=bool(reasons), reasons=reasons, forced_outcome=forced)

    def commit(self, gw: ToolGateway, ctx: CallContext, case_id: str, final: dict[str, Any],
               idempotency_key: str) -> dict[str, Any]:
        return gw.call("record_decision", {"alert_id": case_id, "outcome": final["outcome"],
                                           "rationale": final["rationale"], "citations": final["citations"],
                                           "decided_by": final["decided_by"]}, ctx, idempotency_key=idempotency_key)


def build_retriever() -> BM25Index:
    chunks = []
    for p in sorted(KNOWLEDGE.glob("*.md")):
        chunks += chunk_markdown(p.stem, p.read_text())
    return BM25Index(chunks)


def build_runner(world: dict[str, Any] | None = None, checkpointer: Any = None,
                 gateway: ToolGateway | None = None) -> tuple[CaseRunner, ToolGateway, Store]:
    from langgraph.checkpoint.memory import InMemorySaver

    store = Store(world or build_world())
    gw = gateway or ToolGateway(build_policy(), AuditLog())
    gw.register(*build_tools(store))
    rule = RoutingRule(task="aml_triage", model="claude-opus-5-5", effort="medium")
    graph = build_decision_graph(AmlAdapter(), gw, build_retriever(),
                                 model_factory=lambda: build_decision_model(rule, heuristic),
                                 checkpointer=checkpointer or InMemorySaver())
    return CaseRunner(graph, AmlAdapter.reviewer_roles), gw, store
