"""Golden-path LangGraph workflow for "decide against a policy" agents.

All three portfolio agents (AML alert triage, prior authorization, underwriting triage) are the
same shape of problem: gather facts, read the policy, propose a decision, apply hard rules,
ask a human when required, then write to the system of record exactly once. The *shape* is
fixed code (cheaper, testable, auditable); the LLM is used only where judgement is needed.

    intake -> investigate -> retrieve -> propose -> guard --(auto)--> commit -> END
                                                       \\--(human)--> human_review -> commit

Durability: each node boundary is a checkpoint. With a Postgres checkpointer a crashed worker
is resumed by re-invoking the same thread_id; completed nodes are not re-run and the commit is
idempotent, so "exactly-once effect" holds even with at-least-once execution.

Human in the loop: `human_review` calls `interrupt()`. The run parks (no compute held) until
the API resumes it with `Command(resume={...})`, possibly days later and on another worker.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from pydantic import BaseModel, Field

from .gateway import CallContext, ToolGateway
from .guardrails import SPOTLIGHT_SYSTEM_RULE, RedactionVault, redact, scan_for_injection, spotlight
from .models import BudgetExceeded, CostMeter, DecisionModel, ModelRefusal
from .retrieval import Evidence, Retriever, gather_evidence


class Proposal(BaseModel):
    """Base schema every domain proposal extends (domains narrow `outcome` to a Literal)."""

    outcome: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=4000)
    citations: list[str] = Field(default_factory=list, description="Ids like 'doc#chunk' from <evidence>")
    injection_observed: bool = False


@dataclass(frozen=True)
class GuardOutcome:
    require_human: bool
    reasons: list[str] = field(default_factory=list)
    forced_outcome: str | None = None


class DomainAdapter(Protocol):
    name: str
    proposal_schema: type[Proposal]
    system_prompt: str
    agent_role: str
    reviewer_roles: dict[str, set[str]]      # outcome -> roles allowed to sign it off
    auto_outcomes: set[str]                  # outcomes the agent may commit without a human
    min_auto_confidence: float
    transient_keys: set[str]                 # fact keys holding raw untrusted text; dropped after intake

    def load(self, case_id: str, gw: ToolGateway, ctx: CallContext) -> dict[str, Any]: ...
    def untrusted_text(self, facts: dict[str, Any]) -> str: ...
    def queries(self, facts: dict[str, Any]) -> tuple[list[str], dict[str, Any]]: ...
    def render(self, facts: dict[str, Any], evidence: str, untrusted_block: str) -> str: ...
    def heuristic(self, ctx: dict[str, Any]) -> dict[str, Any]: ...
    def hard_rules(self, facts: dict[str, Any], proposal: dict[str, Any]) -> GuardOutcome: ...
    def commit(self, gw: ToolGateway, ctx: CallContext, case_id: str, final: dict[str, Any],
               idempotency_key: str) -> dict[str, Any]: ...


class CaseState(TypedDict):
    case_id: str
    trace_id: str
    facts: NotRequired[dict[str, Any]]
    untrusted_block: NotRequired[str]
    injection: NotRequired[dict[str, Any]]
    evidence: NotRequired[dict[str, Any]]
    proposal: NotRequired[dict[str, Any]]
    guard: NotRequired[dict[str, Any]]
    review: NotRequired[dict[str, Any]]
    final: NotRequired[dict[str, Any]]
    receipt: NotRequired[dict[str, Any]]
    usage: NotRequired[dict[str, Any]]
    status: NotRequired[Literal["running", "awaiting_human", "committed", "failed"]]


class ReviewRejected(ValueError):
    pass


def validate_review(reviewer_roles: dict[str, set[str]], decision: dict[str, Any] | None,
                    proposed: str) -> str:
    """Return an error message, or "" if the sign-off is acceptable."""
    if not isinstance(decision, dict):
        return "review payload must be an object"
    outcome = decision.get("outcome") or proposed
    if outcome not in reviewer_roles:
        return f"unknown outcome {outcome!r}"
    if decision.get("role") not in reviewer_roles[outcome]:
        return f"role {decision.get('role')!r} may not sign off {outcome!r}"
    if not decision.get("reviewer") or not decision.get("note"):
        return "reviewer id and a note are required for the audit trail"
    return ""


def build_decision_graph(adapter: DomainAdapter, gw: ToolGateway, retriever: Retriever,
                         model_factory: Callable[[], DecisionModel], checkpointer: Any,
                         budget_usd: float = 0.50) -> Any:
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def ctx_for(state: CaseState, role: str | None = None, principal: str | None = None,
                **attrs: Any) -> CallContext:
        return CallContext(principal or f"svc-{adapter.name}", role or adapter.agent_role,
                           state["trace_id"], attrs)

    # -- nodes ---------------------------------------------------------------------------
    def intake(state: CaseState) -> dict[str, Any]:
        facts = adapter.load(state["case_id"], gw, ctx_for(state))
        raw = adapter.untrusted_text(facts)
        # Raw untrusted text (may hold PII/PHI) never enters checkpointed state; only the
        # redacted, spotlighted block does.
        facts = {k: v for k, v in facts.items() if k not in adapter.transient_keys}
        inj = scan_for_injection(raw)
        # Redact before the text can reach a prompt, a checkpoint or a log.
        block, _ = spotlight(redact(raw, RedactionVault()), source="case_untrusted_text")
        return {"facts": facts, "untrusted_block": block, "status": "running",
                "injection": {"score": inj.score, "signals": list(inj.signals), "suspicious": inj.suspicious}}

    def investigate(state: CaseState) -> dict[str, Any]:
        # Domain adapters gather their facts in `load`; this node exists so enrichment that is
        # slow or flaky (bureaus, EHR, third-party data) gets its own checkpoint boundary.
        enrich = getattr(adapter, "enrich", None)
        if enrich is None:
            return {}
        return {"facts": {**state["facts"], **enrich(state["facts"], gw, ctx_for(state))}}

    def retrieve(state: CaseState) -> dict[str, Any]:
        queries, filters = adapter.queries(state["facts"])
        ev: Evidence = gather_evidence(retriever, queries, filters)
        return {"evidence": {"citations": ev.citations, "context": ev.as_context(),
                             "sufficient": ev.sufficient, "queries": list(ev.queries)}}

    def propose(state: CaseState) -> dict[str, Any]:
        model = model_factory()
        meter = CostMeter(budget_usd)
        context = {"facts": state["facts"], "evidence": state["evidence"], "injection": state["injection"]}
        user = adapter.render(state["facts"], state["evidence"]["context"], state["untrusted_block"])
        system = adapter.system_prompt + "\n\n" + SPOTLIGHT_SYSTEM_RULE
        try:
            p, inp, out = model.propose(system, user, adapter.proposal_schema, context)
            meter.charge(model.profile, inp, out)
            proposal = p.model_dump()
        except (ModelRefusal, BudgetExceeded, ValueError) as e:
            # Fail safe: no model opinion -> a human decides. Never fail open.
            proposal = {"outcome": "NEEDS_HUMAN", "confidence": 0.0, "rationale": f"model unavailable: {e}",
                        "citations": [], "injection_observed": False}
        usage = {"model": model.profile.id, "input_tokens": meter.usage.input_tokens,
                 "output_tokens": meter.usage.output_tokens, "cost_usd": meter.usage.cost_usd}
        return {"proposal": proposal, "usage": usage}

    def guard(state: CaseState) -> dict[str, Any]:
        p = dict(state["proposal"])
        reasons: list[str] = []
        valid = set(state["evidence"]["citations"])
        bad = [c for c in p.get("citations", []) if c not in valid]
        if bad:
            reasons.append(f"citations not in retrieved evidence: {bad}")
            p["citations"] = [c for c in p["citations"] if c in valid]
        if not p.get("citations"):
            reasons.append("no valid policy citation")
        if not state["evidence"]["sufficient"]:
            reasons.append("insufficient policy evidence retrieved")
        if state["injection"]["suspicious"] or p.get("injection_observed"):
            reasons.append(f"possible prompt injection in case text: {state['injection']['signals']}")
        if p["outcome"] not in adapter.auto_outcomes:
            reasons.append(f"outcome {p['outcome']} requires human sign-off")
        if p.get("confidence", 0) < adapter.min_auto_confidence:
            reasons.append(f"confidence {p.get('confidence')} below {adapter.min_auto_confidence}")
        hr = adapter.hard_rules(state["facts"], p)
        reasons += hr.reasons
        if hr.forced_outcome:
            p["outcome"] = hr.forced_outcome
        # Policy pre-check: would the agent's own identity be allowed to commit this?
        pre = gw.policy.evaluate(adapter.agent_role, "record_decision",
                                 {**state["facts"].get("policy_attrs", {}), "outcome": p["outcome"]})
        if not pre.allowed:
            reasons.append("policy: " + "; ".join(pre.reasons))
        need_human = bool(reasons) or hr.require_human
        return {"proposal": p, "guard": {"require_human": need_human, "reasons": reasons}}

    def human_review(state: CaseState) -> dict[str, Any]:
        # Validation loop: an invalid sign-off re-parks the run with the error instead of failing it.
        # (Each interrupt() call is matched to its own resume value, so a rejected value is not reused.)
        error = ""
        while True:
            decision = interrupt({
                "case_id": state["case_id"], "proposal": state["proposal"], "reasons": state["guard"]["reasons"],
                "allowed": {k: sorted(v) for k, v in adapter.reviewer_roles.items()},
                "evidence": state["evidence"]["citations"], "error": error,
            })
            error = validate_review(adapter.reviewer_roles, decision, state["proposal"]["outcome"])
            if not error:
                break
        outcome = decision.get("outcome") or state["proposal"]["outcome"]
        role, reviewer = decision["role"], decision["reviewer"]
        return {"review": {"reviewer": reviewer, "role": role, "note": decision["note"][:2000],
                           "agreed_with_agent": outcome == state["proposal"]["outcome"]},
                "final": {"outcome": outcome, "decided_by": reviewer, "role": role,
                          "rationale": state["proposal"]["rationale"], "citations": state["proposal"]["citations"]},
                "status": "running"}

    def auto_final(state: CaseState) -> dict[str, Any]:
        p = state["proposal"]
        return {"final": {"outcome": p["outcome"], "decided_by": f"svc-{adapter.name}", "role": adapter.agent_role,
                          "rationale": p["rationale"], "citations": p["citations"]}}

    def commit(state: CaseState) -> dict[str, Any]:
        f = state["final"]
        ctx = ctx_for(state, role=f["role"], principal=f["decided_by"], **state["facts"].get("policy_attrs", {}))
        key = f"{adapter.name}:{state['case_id']}:{f['outcome']}"
        receipt = adapter.commit(gw, ctx, state["case_id"], f, key)
        return {"receipt": receipt, "status": "committed"}

    def route(state: CaseState) -> str:
        return "human_review" if state["guard"]["require_human"] else "auto_final"

    g = StateGraph(CaseState)
    for name, fn in [("intake", intake), ("investigate", investigate), ("retrieve", retrieve),
                     ("propose", propose), ("guard", guard), ("human_review", human_review),
                     ("auto_final", auto_final), ("commit", commit)]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "investigate")
    g.add_edge("investigate", "retrieve")
    g.add_edge("retrieve", "propose")
    g.add_edge("propose", "guard")
    g.add_conditional_edges("guard", route, {"human_review": "human_review", "auto_final": "auto_final"})
    g.add_edge("human_review", "commit")
    g.add_edge("auto_final", "commit")
    g.add_edge("commit", END)
    return g.compile(checkpointer=checkpointer)


class CaseRunner:
    """Thin façade used by the API, the AgentCore entrypoint, evals and tests."""

    def __init__(self, graph: Any, reviewer_roles: dict[str, set[str]]):
        self.graph = graph
        self.reviewer_roles = reviewer_roles

    @staticmethod
    def config(case_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"case-{case_id}"}}

    def start(self, case_id: str) -> dict[str, Any]:
        state = {"case_id": case_id, "trace_id": uuid.uuid4().hex}
        return self._summarise(case_id, self.graph.invoke(state, self.config(case_id)))

    def resume(self, case_id: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resume after a human decision, or (decision=None) after a crash."""
        from langgraph.types import Command

        if decision is not None:
            snap = self.snapshot(case_id)
            if snap["status"] != "awaiting_human":
                raise ReviewRejected(f"case {case_id} is {snap['status']}, not awaiting review")
            err = validate_review(self.reviewer_roles, decision, snap["proposal"]["outcome"])
            if err:
                raise ReviewRejected(err)  # rejected before touching the graph
        payload = Command(resume=decision) if decision is not None else None
        return self._summarise(case_id, self.graph.invoke(payload, self.config(case_id)))

    def snapshot(self, case_id: str) -> dict[str, Any]:
        return self._summarise(case_id, self.graph.get_state(self.config(case_id)).values)

    def _summarise(self, case_id: str, values: dict[str, Any]) -> dict[str, Any]:
        snap = self.graph.get_state(self.config(case_id))
        pending = [i.value for t in snap.tasks for i in (t.interrupts or ())]
        status = "awaiting_human" if pending else values.get("status", "running")
        return {"case_id": case_id, "status": status, "pending_review": pending[0] if pending else None,
                "proposal": values.get("proposal"), "guard": values.get("guard"), "final": values.get("final"),
                "receipt": values.get("receipt"), "usage": values.get("usage"),
                "citations": (values.get("final") or values.get("proposal") or {}).get("citations", [])}
