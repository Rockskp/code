import re
from pathlib import Path

import pytest
from agentkit.decision_graph import ReviewRejected
from fastapi.testclient import TestClient

from sentinel.agent import build_runner
from sentinel.policies import RULE_IDS, build_policy
from sentinel.synthetic import build_world
from sentinel.tools import compute_signals, name_similarity

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env():
    return build_runner(world=build_world())


def test_signals_structuring_and_velocity():
    w = build_world()
    s = compute_signals(w["transactions"]["C01"], w["customers"]["C01"])
    assert s["structuring_deposits_14d"] == 4
    s8 = compute_signals(w["transactions"]["C08"], w["customers"]["C08"])
    assert s8["pass_through_ratio"] == 0.95 and s8["velocity_ratio"] > 3


def test_sanctions_similarity_bands():
    assert name_similarity("Volkov Trading FZ-E", "Volkov Trading FZE") >= 0.85
    assert 0.70 <= name_similarity("Kestrel Marine Holding Co", "Kestrel Maritime Holdings") < 0.85
    assert name_similarity("Acme Bakery", "Arash Petrochem Ltd") < 0.6


def test_low_risk_false_positive_auto_closes(env):
    runner, gw, store = env
    out = runner.start("F03")
    assert out["status"] == "committed" and out["final"]["outcome"] == "CLOSE"
    assert store.w["dispositions"]["F03"][0]["decided_by"] == "svc-sentinel"
    assert gw.audit.verify()


def test_sar_requires_bsa_officer(env):
    runner, gw, store = env
    out = runner.start("F01")
    assert out["status"] == "awaiting_human" and out["pending_review"]["proposal"]["outcome"] == "SAR"
    with pytest.raises(ReviewRejected):
        runner.resume("F01", {"outcome": "SAR", "role": "aml_analyst", "reviewer": "ana", "note": "agree"})
    assert runner.snapshot("F01")["status"] == "awaiting_human"  # still parked, nothing written
    assert "F01" not in store.w["dispositions"]
    done = runner.resume("F01", {"outcome": "SAR", "role": "bsa_officer", "reviewer": "bo-7",
                                 "note": "Four sub-threshold deposits; file SAR."})
    assert done["status"] == "committed"
    assert store.w["dispositions"]["F01"][0]["decided_by"] == "bo-7"


def test_injection_does_not_change_outcome(env):
    runner, _, _ = env
    out = runner.start("F15")
    assert out["proposal"]["outcome"] == "SAR"
    assert any("prompt injection" in r for r in out["guard"]["reasons"])


def test_pep_close_needs_analyst(env):
    runner, _, _ = env
    out = runner.start("F13")
    assert out["status"] == "awaiting_human"
    assert any("PEP" in r for r in out["guard"]["reasons"])


def test_crash_recovery_resumes_without_rerunning_completed_nodes(env):
    runner, gw, store = env
    real = gw._tools["record_decision"].fn
    state = {"n": 0}

    def crash_once(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("worker lost")
        return real(**kw)

    object.__setattr__(gw._tools["record_decision"], "fn", crash_once)
    with pytest.raises(RuntimeError):
        runner.start("F21")
    reads_before = sum(e.tool == "get_alert" for e in gw.audit.entries)
    out = runner.resume("F21")  # same thread id, no new input
    assert out["status"] == "committed"
    assert sum(e.tool == "get_alert" for e in gw.audit.entries) == reads_before  # intake not re-run
    assert len(store.w["dispositions"]["F21"]) == 1


def test_policy_parity_with_cedar():
    cedar = (ROOT / "policies" / "aml.cedar").read_text()
    assert set(re.findall(r'@id\("([^"]+)"\)', cedar)) == RULE_IDS
    assert {r.id for r in build_policy().rules} == RULE_IDS


def test_api_review_flow(env, monkeypatch):
    from agentkit.service import create_app

    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    runner, _, _ = env
    c = TestClient(create_app(runner, "t", {"aml_analyst", "bsa_officer"}))
    h = {"X-Api-Key": "test-key", "X-User": "ana", "X-Role": "aml_analyst"}
    assert c.post("/cases/F14/run", headers={**h, "X-Api-Key": "bad"}).status_code == 401
    assert c.post("/cases/F14/run", headers=h).json()["status"] == "awaiting_human"
    r = c.post("/cases/F14/review", headers=h, json={"outcome": "CLOSE", "note": "Volume matches KYC profile."})
    assert r.status_code == 200 and r.json()["status"] == "committed"
    assert c.post("/cases/F14/review", headers=h, json={"outcome": "CLOSE", "note": "again, duplicate"}).status_code == 409


def test_mcp_server_exposes_reads_not_writes(env):
    import asyncio

    from agentkit.mcp_server import build_mcp_server

    from sentinel.agent import build_retriever

    _, gw, _ = env
    srv = build_mcp_server("sentinel", gw, build_retriever(), "aml_agent")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert "search_policy" in names and "get_alert" in names and "record_decision" not in names


def test_invalid_signoff_reaching_graph_reparks_run(env):
    """Defence in depth: even if a caller bypasses CaseRunner validation, the node re-interrupts."""
    from langgraph.types import Command

    runner, _, store = env
    runner.start("F06")
    runner.graph.invoke(Command(resume={"outcome": "CLOSE", "role": "aml_agent", "reviewer": "x", "note": "n"}),
                        runner.config("F06"))
    snap = runner.snapshot("F06")
    assert snap["status"] == "awaiting_human" and "may not sign off" in snap["pending_review"]["error"]
    assert "F06" not in store.w["dispositions"]
