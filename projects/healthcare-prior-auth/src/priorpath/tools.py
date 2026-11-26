"""PriorPath tools. Criteria evaluation over *structured* EHR findings is deterministic code;
the model's job is reading the free-text note for criteria the structured data marks `unknown`."""

from __future__ import annotations

from typing import Any, Literal

from agentkit import Capability, ToolError, ToolSpec
from pydantic import BaseModel, ConfigDict, Field

Determination = Literal["APPROVE", "PEND_INFO", "DENY"]
Status = Literal["met", "not_met", "unknown"]

RED_FLAGS = {"cauda_equina", "progressive_neuro_deficit", "suspected_malignancy", "suspected_infection", "trauma"}
COMORBIDITIES = {"type2_diabetes", "hypertension", "dyslipidemia", "obstructive_sleep_apnea"}
HST_UNRELIABLE = {"heart_failure", "copd_moderate_severe", "neuromuscular_disease", "non_apnea_sleep_disorder"}


class Store:
    def __init__(self, world: dict[str, Any]):
        self.w = world

    def get(self, table: str, key: str) -> dict[str, Any]:
        try:
            return dict(self.w[table][key])
        except KeyError as e:
            raise ToolError(f"unknown {table} {key}") from e


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReqArgs(_Strict):
    request_id: str = Field(pattern=r"^[A-Z0-9\-]{2,20}$")


class MemberArgs(_Strict):
    member_id: str = Field(pattern=r"^[A-Z0-9\-]{2,20}$")


class CriteriaArgs(_Strict):
    service: Literal["MRI_LUMBAR", "TKA", "GLP1", "PSG"]
    findings: dict[str, Any]


class RecordArgs(_Strict):
    request_id: str
    outcome: Determination
    rationale: str = Field(min_length=20, max_length=4000)
    citations: list[str] = Field(min_length=1)
    decided_by: str


def _overall(c: dict[str, Status], rule: str = "all") -> Status:
    vals = list(c.values())
    if rule == "all":
        if "not_met" in vals:
            return "not_met"
        return "unknown" if "unknown" in vals else "met"
    return "met" if "met" in vals else ("unknown" if "unknown" in vals else "not_met")


def evaluate_criteria(service: str, f: dict[str, Any]) -> dict[str, Any]:
    c: dict[str, Status] = {}
    if service == "MRI_LUMBAR":
        flags = set(f.get("red_flags") or []) & RED_FLAGS
        wk = f.get("conservative_therapy_weeks")
        prior = f.get("prior_lumbar_mri_months")
        c["red_flag"] = "met" if flags else "unknown"
        c["conservative_6wk"] = "unknown" if wk is None else ("met" if wk >= 6 else "not_met")
        repeat_block = prior is not None and prior < 12 and not flags and not f.get("new_symptoms")
        c["no_recent_mri"] = "not_met" if repeat_block else "met"
        if c["no_recent_mri"] == "not_met":
            overall: Status = "not_met"
        elif flags:
            overall = "met"
        else:
            overall = c["conservative_6wk"]
    elif service == "TKA":
        kl, wk, bmi = f.get("kl_grade"), f.get("conservative_therapy_weeks"), f.get("bmi")
        types = set(f.get("therapy_types") or [])
        c["kl_grade_3_4"] = "unknown" if kl is None else ("met" if kl >= 3 else "not_met")
        c["conservative_12wk"] = "unknown" if wk is None else ("met" if wk >= 12 and "pt" in types and
                                                               types & {"nsaid", "injection"} else "not_met")
        c["functional_limitation"] = "met" if f.get("functional_limitation") else "unknown"
        c["bmi_below_40"] = "unknown" if bmi is None else ("met" if bmi < 40 else "not_met")
        overall = _overall(c)
    elif service == "GLP1":
        bmi, com = f.get("bmi"), set(f.get("comorbidities") or []) & COMORBIDITIES
        months = f.get("lifestyle_program_months")
        c["bmi_threshold"] = "unknown" if bmi is None else (
            "met" if bmi >= 30 or (bmi >= 27 and com) else "not_met")
        c["lifestyle_3mo"] = "unknown" if months is None else ("met" if months >= 3 else "not_met")
        c["no_contraindication"] = "not_met" if f.get("mtc_men2_history") else "met"
        overall = _overall(c)
    elif service == "PSG":
        hst = f.get("hst_result")
        cond = set(f.get("conditions") or []) & HST_UNRELIABLE
        c["hst_inconclusive"] = "met" if hst == "inconclusive" else ("not_met" if hst else "unknown")
        c["hst_unreliable_condition"] = "met" if cond else "unknown"
        overall = "met" if "met" in c.values() else "not_met"
    else:
        raise ToolError(f"no criteria for {service}")
    return {"service": service, "criteria": c, "overall": overall}


def build_tools(store: Store) -> list[ToolSpec]:
    roles = frozenset({"pa_agent", "um_nurse", "medical_director"})

    def get_request(request_id: str) -> dict[str, Any]:
        return store.get("requests", request_id)

    def get_eligibility(member_id: str) -> dict[str, Any]:
        m = store.get("members", member_id)
        return {"member_id": member_id, "coverage_active": m["coverage_active"], "plan": m["plan"], "age": m["age"]}

    def get_clinical_record(request_id: str) -> dict[str, Any]:
        return store.get("records", request_id)

    def check_criteria(service: str, findings: dict[str, Any]) -> dict[str, Any]:
        return evaluate_criteria(service, findings)

    def record_determination(request_id: str, outcome: str, rationale: str, citations: list[str],
                             decided_by: str) -> dict[str, Any]:
        store.get("requests", request_id)
        rec = {"request_id": request_id, "determination": outcome, "decided_by": decided_by,
               "citations": citations, "rationale": rationale}
        store.w["determinations"].setdefault(request_id, []).append(rec)
        return {"determination_id": f"UM-{request_id}-{len(store.w['determinations'][request_id])}", **rec}

    R, U = Capability.READS_PRIVATE, Capability.INGESTS_UNTRUSTED
    return [
        ToolSpec("get_request", "Prior auth request: service, code, urgency, provider.", ReqArgs, get_request,
                 frozenset({R}), roles),
        ToolSpec("get_eligibility", "Coverage status for the member (minimum necessary fields).", MemberArgs,
                 get_eligibility, frozenset({R}), roles),
        ToolSpec("get_clinical_record", "Structured findings + free-text clinical note (PHI, untrusted).", ReqArgs,
                 get_clinical_record, frozenset({R, U}), roles),
        ToolSpec("check_criteria", "Evaluate plan criteria against structured findings.", CriteriaArgs,
                 check_criteria, frozenset(), roles),
        ToolSpec("record_decision", "Write the UM determination (idempotent).", RecordArgs,
                 record_determination, frozenset({Capability.WRITES}), roles),
    ]
