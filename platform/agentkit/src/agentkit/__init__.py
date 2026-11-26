"""agentkit: shared, domain-agnostic controls for the three portfolio agents."""

from .gateway import (
    AuditLog,
    CallContext,
    Capability,
    LethalTrifectaError,
    PolicyDenied,
    ToolError,
    ToolGateway,
    ToolSpec,
    TransientToolError,
)
from .guardrails import (
    SPOTLIGHT_SYSTEM_RULE,
    InjectionReport,
    RedactionVault,
    contains_pii,
    redact,
    scan_for_injection,
    spotlight,
)
from .models import (
    CATALOG,
    BudgetExceeded,
    CostMeter,
    DecisionModel,
    HeuristicDecisionModel,
    ModelRefusal,
    RoutingRule,
    build_decision_model,
)
from .policy import Mode, PolicyEngine, forbid, permit
from .retrieval import BM25Index, Chunk, Evidence, chunk_markdown, gather_evidence

__all__ = [
    "AuditLog", "BM25Index", "BudgetExceeded", "CATALOG", "CallContext", "Capability", "Chunk",
    "CostMeter", "DecisionModel", "Evidence", "HeuristicDecisionModel", "InjectionReport",
    "LethalTrifectaError", "Mode", "ModelRefusal", "PolicyDenied", "PolicyEngine", "RedactionVault",
    "RoutingRule", "SPOTLIGHT_SYSTEM_RULE", "ToolError", "ToolGateway", "ToolSpec", "TransientToolError",
    "build_decision_model", "chunk_markdown", "contains_pii", "forbid", "gather_evidence", "permit",
    "redact", "scan_for_injection", "spotlight",
]
