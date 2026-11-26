"""Tools: deterministic business logic exposed to the agent through the gateway (and MCP).

Rule of thumb from ADR-0002: anything that is arithmetic, a threshold or a lookup is code.
The model reads the results; it never computes a ratio or decides a threshold itself.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any, Literal

from agentkit import Capability, ToolError, ToolSpec
from pydantic import BaseModel, ConfigDict, Field

from .synthetic import AS_OF, HIGH_RISK_COUNTRIES, SANCTIONS_LIST

Outcome = Literal["CLOSE", "ESCALATE", "SAR"]


class Store:
    """Stand-in for core banking + case management. Swap for real adapters behind the same tools."""

    def __init__(self, world: dict[str, Any]):
        self.w = world

    def alert(self, alert_id: str) -> dict[str, Any]:
        try:
            return dict(self.w["alerts"][alert_id])
        except KeyError as e:
            raise ToolError(f"unknown alert {alert_id}") from e


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlertArgs(_Strict):
    alert_id: str = Field(pattern=r"^[A-Z0-9\-]{2,20}$")


class CustomerArgs(_Strict):
    customer_id: str = Field(pattern=r"^[A-Z0-9\-]{2,20}$")


class TxnArgs(CustomerArgs):
    days: int = Field(default=30, ge=1, le=90)


class ScreenArgs(_Strict):
    names: list[str] = Field(max_length=50)


class SignalArgs(CustomerArgs):
    pass


class RecordArgs(_Strict):
    alert_id: str
    outcome: Outcome
    rationale: str = Field(min_length=20, max_length=4000)
    citations: list[str] = Field(min_length=1)
    decided_by: str


_LEGAL = re.compile(r"\b(ltd|llc|sa|fze|fz-e|co|inc|holding|holdings|plc|gmbh)\b\.?")


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("fz-e", "fze"))
    return " ".join(n.split())


def name_similarity(a: str, b: str) -> float:
    """Blend of full-string and core-name similarity (legal suffixes stripped)."""
    full = SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    core = SequenceMatcher(None, " ".join(_LEGAL.sub("", _norm(a)).split()),
                           " ".join(_LEGAL.sub("", _norm(b)).split())).ratio()
    return round(max(full, core * 0.95), 3)


def compute_signals(txns: list[dict[str, Any]], cust: dict[str, Any], as_of: date = AS_OF) -> dict[str, Any]:
    def age(t: dict[str, Any]) -> int:
        return (as_of - date.fromisoformat(t["date"])).days

    last30 = [t for t in txns if age(t) <= 30]
    cash = sorted((t for t in txns if t["type"] == "cash_deposit" and 8_000 <= t["amount"] <= 9_999 and age(t) <= 14),
                  key=lambda t: t["date"])
    wires_in = [t for t in last30 if t["type"] == "wire_in"]
    out_after = 0
    for w in wires_in:
        d0 = date.fromisoformat(w["date"])
        out_after += sum(t["amount"] for t in last30 if t["type"] == "wire_out"
                         and d0 <= date.fromisoformat(t["date"]) <= d0 + timedelta(days=3))
    inflow = sum(w["amount"] for w in wires_in)
    volume = sum(t["amount"] for t in last30 if t["type"] != "refund")
    travel = set(cust.get("travel_notice_countries", []))
    foreign = [t for t in last30 if t["country"] != cust.get("country", "US")]
    return {
        "structuring_deposits_14d": len(cash),
        "pass_through_ratio": round(out_after / inflow, 3) if inflow else 0.0,
        "velocity_ratio": round(volume / max(1, cust["expected_monthly_volume"]), 2),
        "volume_30d": volume,
        "high_risk_geo_txns": sum(1 for t in last30 if t["country"] in HIGH_RISK_COUNTRIES),
        "foreign_card_unexplained": sum(1 for t in foreign if t["type"] == "card" and t["country"] not in travel),
        "foreign_card_covered_by_travel_notice": sum(1 for t in foreign if t["type"] == "card" and t["country"] in travel),
    }


def build_tools(store: Store) -> list[ToolSpec]:
    def get_alert(alert_id: str) -> dict[str, Any]:
        return store.alert(alert_id)

    def get_customer(customer_id: str) -> dict[str, Any]:
        c = store.w["customers"].get(customer_id)
        if not c:
            raise ToolError(f"unknown customer {customer_id}")
        return dict(c)

    def get_transactions(customer_id: str, days: int = 30) -> list[dict[str, Any]]:
        cutoff = AS_OF - timedelta(days=days)
        return [t for t in store.w["transactions"].get(customer_id, []) if date.fromisoformat(t["date"]) >= cutoff]

    def screen_sanctions(names: list[str]) -> dict[str, Any]:
        best = {"name": None, "match": None, "score": 0.0}
        for n in names:
            for s in SANCTIONS_LIST:
                sc = name_similarity(n, s)
                if sc > best["score"]:
                    best = {"name": n, "match": s, "score": sc}
        return best

    def risk_signals(customer_id: str) -> dict[str, Any]:
        return compute_signals(get_transactions(customer_id, 30), get_customer(customer_id))

    def record_decision(alert_id: str, outcome: str, rationale: str, citations: list[str], decided_by: str) -> dict:
        store.alert(alert_id)
        rec = {"alert_id": alert_id, "outcome": outcome, "decided_by": decided_by, "citations": citations,
               "rationale": rationale, "version": len(store.w["dispositions"].get(alert_id, [])) + 1}
        store.w["dispositions"].setdefault(alert_id, []).append(rec)
        return {"disposition_id": f"DSP-{alert_id}-{rec['version']}", **rec}

    R, U = Capability.READS_PRIVATE, Capability.INGESTS_UNTRUSTED
    readers = frozenset({"aml_agent", "aml_analyst", "bsa_officer"})
    return [
        ToolSpec("get_alert", "Fetch an alert, including the free-text memo (untrusted).", AlertArgs, get_alert,
                 frozenset({R, U}), readers),
        ToolSpec("get_customer", "KYC profile: risk rating, PEP flag, expected volume, notes.", CustomerArgs,
                 get_customer, frozenset({R}), readers),
        ToolSpec("get_transactions", "Transactions for the last N days (max 90).", TxnArgs, get_transactions,
                 frozenset({R}), readers),
        ToolSpec("screen_sanctions", "Fuzzy-match counterparty names against the internal sanctions list.",
                 ScreenArgs, screen_sanctions, frozenset({R}), readers),
        ToolSpec("risk_signals", "Deterministic AML indicators computed from the last 30 days.", SignalArgs,
                 risk_signals, frozenset({R}), readers),
        ToolSpec("record_decision", "Write the alert disposition to case management (idempotent).", RecordArgs,
                 record_decision, frozenset({Capability.WRITES}), readers),
    ]
