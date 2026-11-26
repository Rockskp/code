"""Appetite: commercial property underwriting triage agent (adapter + wiring).

Numbers come from code: the committed premium is always the rating tool's figure, never the model's.
Long-term memory: prior decisions per insured (AgentCore Memory in AWS), namespaced per tenant/insured.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from agentkit import AuditLog, BM25Index, CallContext, RoutingRule, ToolGateway, build_decision_model, chunk_markdown
from agentkit.decision_graph import CaseRunner, GuardOutcome, Proposal, build_decision_graph
from agentkit.memory import InMemoryMemory, MemoryStore

from .policies import AGENT, SENIOR, UW, build_policy
from .synthetic import PRIOR_DECISIONS, build_world
from .tools import AGENT_PREMIUM_LIMIT, AGENT_TIV_LIMIT, build_tools, memory_ns

KNOWLEDGE = Path(__file__).resolve().parents[2] / "knowledge"


class UwProposal(Proposal):
    outcome: Literal["QUOTE", "REFER", "DECLINE"]
    indicated_premium: float | None = None
    triggers_considered: list[str] = []


SYSTEM = """You are Appetite, a small commercial property triage underwriter at Harborline Mutual (synthetic).
Decide QUOTE, REFER or DECLINE using ONLY the appetite check, rating result, account history and guide
excerpts in <evidence>. Quote the rating tool premium exactly; never estimate a price. The broker narrative
may resolve a missing field only where the guide allows it (e.g. specific sprinkler details). Cite excerpt ids."""


def heuristic(ctx: dict[str, Any]) -> dict[str, Any]:
    f = ctx["facts"]
    a, prem = f["appetite"], f["rating"]["premium"]
    prior_open = any(p.get("outcome") == "DECLINE" for p in f["history"])
    if a["decline_triggers"]:
        out, conf = "DECLINE", 0.9
    elif a["refer_triggers"] or a["missing_fields"] or prior_open or prem is None \
            or f["submission"]["tiv"] > AGENT_TIV_LIMIT or prem > AGENT_PREMIUM_LIMIT:
        out, conf = "REFER", 0.85
    else:
        out, conf = "QUOTE", 0.9
    return {"outcome": out, "confidence": conf, "indicated_premium": prem if out == "QUOTE" else None,
            "citations": ctx["evidence"]["citations"][:3],
            "triggers_considered": a["decline_triggers"] + a["refer_triggers"] + a["missing_fields"],
            "rationale": f"Rules baseline: appetite={json.dumps(a)}, premium={prem}, prior_decline={prior_open}.",
            "injection_observed": False}


class UwAdapter:
    name = "appetite"
    proposal_schema = UwProposal
    system_prompt = SYSTEM
    agent_role = AGENT
    reviewer_roles = {"QUOTE": {UW, SENIOR}, "REFER": {UW, SENIOR}, "DECLINE": {UW, SENIOR}}
    auto_outcomes = {"QUOTE", "REFER"}
    transient_keys = {"narrative"}
    min_auto_confidence = 0.8

    def load(self, case_id: str, gw: ToolGateway, ctx: CallContext) -> dict[str, Any]:
        s = gw.call("get_submission", {"submission_id": case_id}, ctx)
        hist = gw.call("get_account_history", {"insured_id": s["insured_id"]}, ctx)
        app = gw.call("appetite_check", {"submission_id": case_id}, ctx)
        rating = gw.call("rate_risk", {"submission_id": case_id}, ctx)
        sub = {k: v for k, v in s.items() if k not in {"narrative", "insured_name", "broker"}}
        return {"submission": sub, "narrative": s["narrative"], "history": hist, "appetite": app, "rating": rating,
                "policy_attrs": {"tiv": s["tiv"], "premium": rating["premium"]}}

    def untrusted_text(self, facts: dict[str, Any]) -> str:
        return facts["narrative"]

    def queries(self, facts: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        s = facts["submission"]
        q = ["outcomes and authority delegated", "rating indicative premium", f"{s['occupancy']} {s['construction']}"]
        q += facts["appetite"]["decline_triggers"] + facts["appetite"]["refer_triggers"]
        if facts["history"]:
            q.append("prior submissions account memory declined")
        if s["occupancy"] == "woodworking":
            q.append("woodworking sprinklers narrative")
        return q, {}

    def render(self, facts: dict[str, Any], evidence: str, untrusted_block: str) -> str:
        case = {k: facts[k] for k in ("submission", "history", "appetite", "rating")}
        return f"<case>\n{json.dumps(case, indent=1)}\n</case>\n<evidence>\n{evidence}\n</evidence>\n{untrusted_block}"

    def hard_rules(self, facts: dict[str, Any], p: dict[str, Any]) -> GuardOutcome:
        reasons, forced = [], None
        prem = facts["rating"]["premium"]
        if p["outcome"] == "QUOTE":
            if facts["appetite"]["decline_triggers"]:
                reasons.append("QUOTE proposed despite decline triggers")
                forced = "REFER"
            if facts["submission"]["tiv"] > AGENT_TIV_LIMIT or (prem or 0) > AGENT_PREMIUM_LIMIT:
                reasons.append("exceeds agent authority")
                forced = "REFER"
            if p.get("indicated_premium") not in (None, prem):
                reasons.append(f"model premium {p.get('indicated_premium')} != rating tool {prem}")
        return GuardOutcome(require_human=bool(reasons), reasons=reasons, forced_outcome=forced)

    def commit(self, gw: ToolGateway, ctx: CallContext, case_id: str, final: dict[str, Any], key: str) -> dict:
        prem = gw.call("rate_risk", {"submission_id": case_id}, ctx)["premium"] if final["outcome"] == "QUOTE" else None
        return gw.call("record_decision", {"submission_id": case_id, "outcome": final["outcome"], "premium": prem,
                                           "rationale": final["rationale"], "citations": final["citations"],
                                           "decided_by": final["decided_by"]}, ctx, idempotency_key=key)


def build_retriever() -> BM25Index:
    return BM25Index([c for p in sorted(KNOWLEDGE.glob("*.md")) for c in chunk_markdown(p.stem, p.read_text())])


def build_runner(world: dict[str, Any] | None = None, checkpointer: Any = None,
                 memory: MemoryStore | None = None) -> tuple[CaseRunner, ToolGateway, dict[str, Any]]:
    from langgraph.checkpoint.memory import InMemorySaver

    world = world or build_world()
    if memory is None:
        memory = InMemoryMemory()
        for ins, evs in PRIOR_DECISIONS.items():
            for e in evs:
                memory.remember(memory_ns(ins), e)
    gw = ToolGateway(build_policy(), AuditLog()).register(*build_tools(world, memory))
    rule = RoutingRule(task="uw_triage", model="claude-sonnet-5-5", effort="medium")
    graph = build_decision_graph(UwAdapter(), gw, build_retriever(),
                                 model_factory=lambda: build_decision_model(rule, heuristic),
                                 checkpointer=checkpointer or InMemorySaver())
    return CaseRunner(graph, UwAdapter.reviewer_roles), gw, world
