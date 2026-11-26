"""Model selection, cost accounting and the structured decision-model interface.

Every agent in this repo asks an LLM for exactly one thing: a *proposal* that validates
against a Pydantic schema. Business rules, limits and side effects live in code (tools +
policy), so the model can be swapped on evidence from evals without touching controls.

Prices are Anthropic first-party list prices per million tokens (cached 2026-11). Bedrock
pricing is set by AWS and can differ: confirm on the Bedrock pricing page before quoting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Tier(StrEnum):
    FRONTIER = "frontier"   # hardest judgement calls, low volume
    BALANCED = "balanced"   # default reasoning / LLM-as-judge
    FAST = "fast"           # extraction, classification, high volume


@dataclass(frozen=True)
class ModelProfile:
    id: str
    tier: Tier
    input_per_mtok: float
    output_per_mtok: float
    bedrock_id: str
    context_tokens: int = 1_000_000

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens / 1e6 * self.input_per_mtok + output_tokens / 1e6 * self.output_per_mtok


CATALOG: dict[str, ModelProfile] = {
    "claude-opus-5-5": ModelProfile("claude-opus-5-5", Tier.FRONTIER, 4.00, 20.00, "anthropic.claude-opus-5-5"),
    "claude-sonnet-5-5": ModelProfile("claude-sonnet-5-5", Tier.BALANCED, 2.00, 10.00, "anthropic.claude-sonnet-5-5"),
    "claude-haiku-5-5": ModelProfile("claude-haiku-5-5", Tier.FAST, 0.10, 0.50, "anthropic.claude-haiku-5-5"),
    # Local / offline baseline: deterministic rules, zero marginal cost, used in CI and as eval floor.
    "heuristic-baseline": ModelProfile("heuristic-baseline", Tier.FAST, 0.0, 0.0, "n/a"),
}


@dataclass(frozen=True)
class RoutingRule:
    """Which model serves which task. Overridable per environment via AGENT_MODEL_<TASK>."""

    task: str
    model: str
    effort: str = "medium"


def resolve_model(rule: RoutingRule) -> tuple[ModelProfile, str]:
    override = os.getenv(f"AGENT_MODEL_{rule.task.upper()}")
    model_id = override or rule.model
    if model_id not in CATALOG:
        raise ValueError(f"model {model_id!r} not in approved catalog")
    return CATALOG[model_id], os.getenv(f"AGENT_EFFORT_{rule.task.upper()}", rule.effort)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, profile: ModelProfile, inp: int, out: int) -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.cost_usd = round(self.cost_usd + profile.cost(inp, out), 6)
        self.calls += 1


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CostMeter:
    budget_usd: float
    usage: Usage = field(default_factory=Usage)

    def charge(self, profile: ModelProfile, inp: int, out: int) -> None:
        self.usage.add(profile, inp, out)
        if self.usage.cost_usd > self.budget_usd:
            raise BudgetExceeded(f"run cost ${self.usage.cost_usd:.4f} exceeds budget ${self.budget_usd}")


class ModelRefusal(RuntimeError):
    """The model declined (stop_reason == 'refusal'). Callers route to human review."""


class DecisionModel(Protocol):
    profile: ModelProfile

    def propose(self, system: str, user: str, schema: type[T], context: dict[str, Any]) -> tuple[T, int, int]:
        """Return (validated proposal, input_tokens, output_tokens)."""
        ...


class HeuristicDecisionModel:
    """Deterministic stand-in for an LLM. `fn(context) -> schema instance`.

    It serves three purposes: offline dev/CI, a cost-zero eval baseline the LLM must beat, and
    a degraded-mode fallback if the model provider is unavailable (decisions then always route
    to a human because the fallback never sets high confidence).
    """

    def __init__(self, fn: Any):
        self.fn = fn
        self.profile = CATALOG["heuristic-baseline"]

    def propose(self, system: str, user: str, schema: type[T], context: dict[str, Any]) -> tuple[T, int, int]:
        out = self.fn(context)
        return schema.model_validate(out if isinstance(out, dict) else out.model_dump()), 0, 0


class AnthropicDecisionModel:
    """Claude via the Anthropic SDK with structured outputs.

    Uses the Bedrock Mantle client when AWS_REGION is set and ANTHROPIC_API_KEY is not (the
    AgentCore deployment path: IAM auth, data stays in the AWS account boundary); otherwise the
    first-party client. Requires `pip install agentkit[llm]`.
    """

    def __init__(self, profile: ModelProfile, effort: str = "medium", max_tokens: int = 4000):
        import anthropic  # lazy: optional dependency

        self.profile = profile
        self.effort = effort
        self.max_tokens = max_tokens
        if os.getenv("AWS_REGION") and not os.getenv("ANTHROPIC_API_KEY"):
            self._client = anthropic.AnthropicBedrockMantle(aws_region=os.environ["AWS_REGION"])
            self._model = profile.bedrock_id
        else:
            self._client = anthropic.Anthropic()
            self._model = profile.id

    def propose(self, system: str, user: str, schema: type[T], context: dict[str, Any]) -> tuple[T, int, int]:
        resp = self._client.messages.parse(
            model=self._model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort},
            output_format=schema,
        )
        if resp.stop_reason == "refusal":
            raise ModelRefusal(str(getattr(resp, "stop_details", "")))
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise ValueError(f"no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output, resp.usage.input_tokens, resp.usage.output_tokens


def build_decision_model(rule: RoutingRule, heuristic_fn: Any) -> DecisionModel:
    """`AGENT_LLM_MODE=live` uses Claude; anything else (default) uses the heuristic baseline."""
    if os.getenv("AGENT_LLM_MODE", "offline") != "live":
        return HeuristicDecisionModel(heuristic_fn)
    profile, effort = resolve_model(rule)
    return AnthropicDecisionModel(profile, effort=effort)
