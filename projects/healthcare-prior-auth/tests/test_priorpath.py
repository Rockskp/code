import json
import re
from pathlib import Path

import pytest
from agentkit.decision_graph import ReviewRejected

from priorpath.agent import PaAdapter, build_runner
from priorpath.policies import RULE_IDS, build_policy
from priorpath.synthetic import build_world
from priorpath.tools import evaluate_criteria

ROOT = Path(__file__).resolve().parents[1]
PHI = re.compile(r"MRN: \d+|DOB: \d{4}|\(555\) 010-|Test Patient")


@pytest.fixture
def env():
    return build_runner(world=build_world())


@pytest.mark.parametrize(("service", "findings", "overall"), [
    ("MRI_LUMBAR", {"conservative_therapy_weeks": 8, "red_flags": []}, "met"),
    ("MRI_LUMBAR", {"conservative_therapy_weeks": None, "red_flags": ["cauda_equina"]}, "met"),
    ("MRI_LUMBAR", {"conservative_therapy_weeks": None, "red_flags": []}, "unknown"),
    ("MRI_LUMBAR", {"conservative_therapy_weeks": 10, "red_flags": [], "prior_lumbar_mri_months": 4}, "not_met"),
    ("TKA", {"kl_grade": 4, "conservative_therapy_weeks": 16, "therapy_types": ["pt", "nsaid"],
             "functional_limitation": True, "bmi": 41}, "not_met"),
    ("GLP1", {"bmi": 28, "comorbidities": ["hypertension"], "lifestyle_program_months": 3,
              "mtc_men2_history": False}, "met"),
    ("PSG", {"hst_result": None, "conditions": []}, "not_met"),
])
def test_criteria_engine(service, findings, overall):
    assert evaluate_criteria(service, findings)["overall"] == overall


def test_meets_criteria_auto_approves(env):
    runner, _, store = env
    out = runner.start("P07")
    assert out["status"] == "committed" and out["final"]["outcome"] == "APPROVE"
    assert store.w["determinations"]["P07"][0]["decided_by"] == "svc-priorpath"


def test_deny_only_by_medical_director(env):
    runner, _, store = env
    out = runner.start("P08")
    assert out["status"] == "awaiting_human" and out["proposal"]["outcome"] == "DENY"
    with pytest.raises(ReviewRejected):
        runner.resume("P08", {"outcome": "DENY", "role": "um_nurse", "reviewer": "rn-1", "note": "BMI 43"})
    done = runner.resume("P08", {"outcome": "DENY", "role": "medical_director", "reviewer": "md-2",
                                 "note": "BMI 43 exceeds criteria; peer-to-peer offered."})
    assert done["status"] == "committed" and store.w["determinations"]["P08"][0]["determination"] == "DENY"


def test_urgent_request_is_never_auto_pended(env):
    runner, _, _ = env
    out = runner.start("P05")
    assert out["status"] == "awaiting_human"
    assert any("expedited" in r for r in out["guard"]["reasons"])


def test_inactive_coverage_forces_admin_deny_with_signoff(env):
    runner, _, _ = env
    out = runner.start("P19")
    assert out["proposal"]["outcome"] == "DENY" and out["status"] == "awaiting_human"


def test_injection_in_note_does_not_flip_deny(env):
    runner, _, _ = env
    out = runner.start("P20")
    assert out["proposal"]["outcome"] == "DENY"
    assert any("prompt injection" in r for r in out["guard"]["reasons"])


def test_phi_never_reaches_prompt_or_checkpoint(env):
    runner, _, _ = env
    runner.start("P01")
    values = runner.graph.get_state(runner.config("P01")).values
    blob = json.dumps(values, default=str)
    assert not PHI.search(blob), PHI.search(blob)
    assert "[MRN_1]" in values["untrusted_block"]
    rendered = PaAdapter().render(values["facts"], values["evidence"]["context"], values["untrusted_block"])
    assert not PHI.search(rendered)


def test_live_mode_refuses_non_bedrock_phi(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MODE", "live")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("PHI_FIRST_PARTY_BAA", raising=False)
    with pytest.raises(RuntimeError, match="PHI"):
        build_runner(world=build_world())


def test_policy_parity_with_cedar():
    cedar = (ROOT / "policies" / "um.cedar").read_text()
    assert set(re.findall(r'@id\("([^"]+)"\)', cedar)) == RULE_IDS
    assert {r.id for r in build_policy().rules} == RULE_IDS
