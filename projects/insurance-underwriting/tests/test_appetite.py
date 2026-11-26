import re
from pathlib import Path

import pytest
from agentkit import AuditLog, Capability, LethalTrifectaError, ToolGateway, ToolSpec
from agentkit.memory import InMemoryMemory

from appetite.agent import build_runner
from appetite.policies import RULE_IDS, build_policy
from appetite.synthetic import build_world
from appetite.tools import build_tools, rate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env():
    return build_runner(world=build_world())


def test_clean_risk_auto_quotes_with_rating_premium(env):
    runner, _, world = env
    out = runner.start("U01")
    assert out["status"] == "committed" and out["final"]["outcome"] == "QUOTE"
    assert world["decisions"]["U01"][0]["premium"] == rate(world["submissions"]["U01"])["premium"]


def test_decline_requires_underwriter(env):
    runner, _, world = env
    assert runner.start("U03")["status"] == "awaiting_human"
    done = runner.resume("U03", {"outcome": "DECLINE", "role": "underwriter", "reviewer": "uw-4",
                                 "note": "Prohibited class: cannabis."})
    assert done["status"] == "committed" and world["decisions"]["U03"][0]["outcome"] == "DECLINE"


def test_authority_limit_premium(env):
    runner, _, _ = env
    assert rate(build_world()["submissions"]["U22"])["premium"] > 50_000
    assert runner.start("U22")["proposal"]["outcome"] == "REFER"


def test_memory_prior_decline_forces_refer_and_new_decision_is_remembered(env):
    runner, gw, _ = env
    assert runner.start("U18")["final"]["outcome"] == "REFER"
    runner.start("U17")
    from agentkit import CallContext
    hist = gw.call("get_account_history", {"insured_id": "INS-17"}, CallContext("t", "uw_agent", "t"))
    assert hist and hist[0]["outcome"] == "QUOTE"


def test_injection_does_not_skip_referral(env):
    runner, _, _ = env
    out = runner.start("U19")
    assert out["proposal"]["outcome"] == "REFER"
    assert any("prompt injection" in r for r in out["guard"]["reasons"])


def test_agent_cannot_email_broker(env):
    _, gw, _ = env
    assert "send_quote_to_broker" not in {t.name for t in gw.tools_for("uw_agent")}


def test_ungated_egress_for_agent_is_rejected():
    world, mem = build_world(), InMemoryMemory()
    tools = build_tools(world, mem)
    send = next(t for t in tools if t.name == "send_quote_to_broker")
    bad = ToolSpec(send.name, send.description, send.args_model, send.fn, send.capabilities,
                   frozenset({"uw_agent"}), requires_human_approval=False)
    readers = [ToolSpec(t.name, t.description, t.args_model, t.fn, t.capabilities | (
        {Capability.READS_PRIVATE} if t.name == "get_submission" else set()), t.roles) for t in tools
        if t.name != "send_quote_to_broker"]
    with pytest.raises(LethalTrifectaError):
        ToolGateway(build_policy(), AuditLog()).register(*readers, bad)


def test_policy_parity_with_cedar():
    cedar = (ROOT / "policies" / "uw.cedar").read_text()
    assert set(re.findall(r'@id\("([^"]+)"\)', cedar)) == RULE_IDS
    assert {r.id for r in build_policy().rules} == RULE_IDS
