"""Appetite tools: appetite rules and rating are code; the model reads the narrative and memory."""

from __future__ import annotations

from typing import Any, Literal

from agentkit import Capability, ToolError, ToolSpec
from agentkit.memory import MemoryStore
from pydantic import BaseModel, ConfigDict, EmailStr, Field

Outcome = Literal["QUOTE", "REFER", "DECLINE"]
TENANT = "harborline"

BASE_RATE = {"office": 0.18, "retail": 0.25, "restaurant": 0.62, "warehouse": 0.30, "woodworking": 0.65}
CONSTRUCTION = {"frame": 1.40, "joisted_masonry": 1.15, "non_combustible": 0.95, "fire_resistive": 0.80}
PROHIBITED = {"cannabis_dispensary", "cannabis_cultivation", "fireworks"}
REQUIRED = ("occupancy", "construction", "tiv", "year_built", "roof_age", "sprinklered", "protection_class")
AGENT_TIV_LIMIT, AGENT_PREMIUM_LIMIT = 5_000_000, 50_000


def memory_ns(insured_id: str) -> str:
    return f"{TENANT}/insured/{insured_id}"


def check_appetite(s: dict[str, Any], as_of_year: int = 2026) -> dict[str, Any]:
    decline, refer, missing = [], [], [k for k in REQUIRED if s.get(k) is None]
    occ = s["occupancy"]
    if occ in PROHIBITED:
        decline.append(f"prohibited class: {occ}")
    if occ == "nightclub" and s.get("alcohol_pct", 0) > 50:
        decline.append("nightclub/bar with alcohol > 50% of sales")
    if s.get("vacancy_pct", 0) > 30:
        decline.append("vacancy above 30%")
    if occ == "woodworking":
        if s.get("sprinklered") is False:
            decline.append("woodworking without sprinklers")
        if s.get("dust_collection") is not True:
            refer.append("woodworking dust collection not confirmed")
    if s.get("wildfire_score", 0) >= 8:
        decline.append("wildfire score >= 8")
    elif s.get("wildfire_score", 0) >= 6:
        refer.append("wildfire score 6-7")
    if s.get("construction") == "frame" and s["tiv"] > 2_000_000:
        refer.append("frame construction with TIV > 2M")
    if (s.get("protection_class") or 0) >= 9 and not s.get("sprinklered"):
        refer.append("protection class 9-10 unsprinklered")
    if as_of_year - s["year_built"] > 50 and (s.get("updates_year") is None or as_of_year - s["updates_year"] > 25):
        refer.append("building > 50 years without updates in last 25 years")
    if (s.get("roof_age") or 0) > 20:
        refer.append("roof age > 20 years")
    if str(s.get("flood_zone", "X"))[:1] in {"A", "V"}:
        refer.append(f"flood zone {s['flood_zone']}")
    losses = s.get("losses", [])
    if len(losses) > 3 or s.get("loss_ratio_5y", 0) > 0.60:
        refer.append("loss frequency/ratio above guideline")
    if any(lo["type"] == "fire" and lo["amount"] > 250_000 and lo["years_ago"] <= 3 for lo in losses):
        refer.append("fire loss > 250k in last 3 years")
    return {"decline_triggers": decline, "refer_triggers": refer, "missing_fields": missing,
            "in_appetite": not decline}


def rate(s: dict[str, Any]) -> dict[str, Any]:
    if s["occupancy"] not in BASE_RATE or s.get("construction") not in CONSTRUCTION:
        return {"premium": None, "reason": "no rate for occupancy/construction"}
    pc = s.get("protection_class") or 10
    factors = {
        "base_per_100": BASE_RATE[s["occupancy"]],
        "construction": CONSTRUCTION[s["construction"]],
        "protection": 0.9 if pc <= 4 else 1.0 if pc <= 6 else 1.2 if pc <= 8 else 1.5,
        "sprinkler": 0.75 if s.get("sprinklered") else 1.0,
        "catastrophe": (1.25 if s.get("wildfire_score", 0) >= 6 else 1.0)
                       * (1.15 if str(s.get("flood_zone", "X"))[:1] in {"A", "V"} else 1.0),
        "loss": round(1 + 0.06 * len(s.get("losses", [])) + 0.5 * max(0.0, s.get("loss_ratio_5y", 0) - 0.3), 3),
    }
    premium = s["tiv"] / 100 * factors["base_per_100"]
    for k in ("construction", "protection", "sprinkler", "catastrophe", "loss"):
        premium *= factors[k]
    return {"premium": round(premium, 2), "factors": factors}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubArgs(_Strict):
    submission_id: str = Field(pattern=r"^[A-Z0-9\-]{2,24}$")


class InsuredArgs(_Strict):
    insured_id: str = Field(pattern=r"^INS-[0-9]{1,8}$")


class RecordArgs(_Strict):
    submission_id: str
    outcome: Outcome
    premium: float | None = None
    rationale: str = Field(min_length=20, max_length=4000)
    citations: list[str] = Field(min_length=1)
    decided_by: str


class SendArgs(_Strict):
    submission_id: str
    to: EmailStr
    body: str = Field(max_length=5000)


def build_tools(world: dict[str, Any], memory: MemoryStore) -> list[ToolSpec]:
    agent_roles = frozenset({"uw_agent", "underwriter", "senior_underwriter"})
    humans = frozenset({"underwriter", "senior_underwriter"})

    def _sub(submission_id: str) -> dict[str, Any]:
        try:
            return dict(world["submissions"][submission_id])
        except KeyError as e:
            raise ToolError(f"unknown submission {submission_id}") from e

    def get_submission(submission_id: str) -> dict[str, Any]:
        return _sub(submission_id)

    def get_account_history(insured_id: str) -> list[dict[str, Any]]:
        return memory.recall(memory_ns(insured_id))

    def appetite_check(submission_id: str) -> dict[str, Any]:
        return check_appetite(_sub(submission_id))

    def rate_risk(submission_id: str) -> dict[str, Any]:
        return rate(_sub(submission_id))

    def record_decision(submission_id: str, outcome: str, premium: float | None, rationale: str,
                        citations: list[str], decided_by: str) -> dict[str, Any]:
        s = _sub(submission_id)
        rec = {"submission_id": submission_id, "outcome": outcome, "premium": premium, "decided_by": decided_by,
               "citations": citations, "rationale": rationale}
        world["decisions"].setdefault(submission_id, []).append(rec)
        memory.remember(memory_ns(s["insured_id"]), {"submission_id": submission_id, "outcome": outcome,
                                                      "reason": rationale[:300]})
        return {"decision_id": f"UW-{submission_id}-{len(world['decisions'][submission_id])}", **rec}

    def send_quote_to_broker(submission_id: str, to: str, body: str) -> dict[str, Any]:
        if world["decisions"].get(submission_id, [{}])[-1].get("outcome") != "QUOTE":
            raise ToolError("no recorded QUOTE for this submission")
        world["outbox"].append({"submission_id": submission_id, "to": to, "body": body})
        return {"queued": True}

    R, U, E, W = (Capability.READS_PRIVATE, Capability.INGESTS_UNTRUSTED, Capability.EXTERNAL_EGRESS,
                  Capability.WRITES)
    return [
        ToolSpec("get_submission", "Broker submission (ACORD-style fields + free-text narrative, untrusted).",
                 SubArgs, get_submission, frozenset({U}), agent_roles),
        ToolSpec("get_account_history", "Prior underwriting decisions for this insured (long-term memory).",
                 InsuredArgs, get_account_history, frozenset({R}), agent_roles),
        ToolSpec("appetite_check", "Deterministic appetite, referral and completeness checks.", SubArgs,
                 appetite_check, frozenset(), agent_roles),
        ToolSpec("rate_risk", "Indicative premium from the rating plan (the only source of price).", SubArgs,
                 rate_risk, frozenset(), agent_roles),
        ToolSpec("record_decision", "Record the triage outcome and premium (idempotent).", RecordArgs,
                 record_decision, frozenset({W}), agent_roles),
        # Egress is human-only AND flagged as approval-gated, so no role holds an un-gated lethal trifecta.
        ToolSpec("send_quote_to_broker", "Email a recorded quote to the broker of record.", SendArgs,
                 send_quote_to_broker, frozenset({E}), humans, requires_human_approval=True),
    ]
