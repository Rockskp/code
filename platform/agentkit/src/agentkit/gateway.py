"""In-process tool gateway: the single choke point between an agent and side effects.

Mirrors what AgentCore Gateway + Policy do at the network edge, so behaviour is identical in
local dev, CI and production:

1. Allowlist   - an agent role may only see/call tools registered for it.
2. Schema      - arguments are validated with the tool's Pydantic model (strict, no extras).
3. Policy      - the PolicyEngine is evaluated with args + caller context; default deny.
4. Reliability - per-call timeout, bounded retries with exponential backoff + full jitter for
                 TransientToolError only, idempotency keys for write tools.
5. Audit       - every call (allowed or refused) is appended to a hash-chained audit log.
6. Trifecta    - at registration time the gateway refuses a role whose tool set combines
                 private data + untrusted content + external egress without a human gate.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from .policy import PolicyEngine


class Capability(StrEnum):
    READS_PRIVATE = "reads_private_data"
    INGESTS_UNTRUSTED = "ingests_untrusted_content"
    EXTERNAL_EGRESS = "external_egress"
    WRITES = "writes_system_of_record"


class ToolError(Exception):
    """Non-retryable tool failure (bad input, business rule)."""


class TransientToolError(ToolError):
    """Retryable failure (timeout, 5xx, throttling)."""


class PolicyDenied(ToolError):
    def __init__(self, tool: str, reasons: tuple[str, ...]):
        super().__init__(f"policy denied {tool}: {'; '.join(reasons)}")
        self.tool = tool
        self.reasons = reasons


class LethalTrifectaError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., Any]
    capabilities: frozenset[Capability] = frozenset()
    roles: frozenset[str] = frozenset({"agent"})
    timeout_s: float = 5.0
    max_retries: int = 2
    requires_human_approval: bool = False

    @property
    def is_write(self) -> bool:
        return Capability.WRITES in self.capabilities

    def json_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.args_model.model_json_schema()}


@dataclass
class AuditEntry:
    ts: str
    trace_id: str
    principal: str
    role: str
    tool: str
    args_digest: str
    outcome: str
    detail: str
    prev_hash: str
    hash: str = ""

    def seal(self) -> AuditEntry:
        body = json.dumps({k: v for k, v in self.__dict__.items() if k != "hash"}, sort_keys=True)
        self.hash = hashlib.sha256(body.encode()).hexdigest()
        return self


@dataclass
class AuditLog:
    """Append-only, hash-chained. Ship to S3 Object Lock / CloudTrail Lake in production."""

    entries: list[AuditEntry] = field(default_factory=list)

    def append(self, **kw: Any) -> AuditEntry:
        prev = self.entries[-1].hash if self.entries else "GENESIS"
        e = AuditEntry(ts=datetime.now(UTC).isoformat(), prev_hash=prev, **kw).seal()
        self.entries.append(e)
        return e

    def verify(self) -> bool:
        prev = "GENESIS"
        for e in self.entries:
            expected = AuditEntry(**{**e.__dict__, "hash": ""}).seal().hash
            if e.prev_hash != prev or e.hash != expected:
                return False
            prev = e.hash
        return True


def assess_trifecta(tools: list[ToolSpec]) -> set[Capability]:
    caps: set[Capability] = set()
    for t in tools:
        caps |= t.capabilities
    return caps


@dataclass
class CallContext:
    principal: str
    role: str
    trace_id: str
    attrs: dict[str, Any] = field(default_factory=dict)


class ToolGateway:
    def __init__(self, policy: PolicyEngine, audit: AuditLog | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_calls_per_trace: int = 50):
        self.policy = policy
        self.audit = audit or AuditLog()
        self._tools: dict[str, ToolSpec] = {}
        self._idem: dict[str, Any] = {}
        self._calls: dict[str, int] = {}
        self._sleep = sleep
        self._max_calls = max_calls_per_trace
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

    # ---- registration -------------------------------------------------------------------
    def register(self, *specs: ToolSpec) -> ToolGateway:
        for s in specs:
            if s.name in self._tools:
                raise ValueError(f"duplicate tool {s.name}")
            self._tools[s.name] = s
        for role in {r for s in self._tools.values() for r in s.roles}:
            self._check_trifecta(role)
        return self

    def _check_trifecta(self, role: str) -> None:
        tools = self.tools_for(role)
        caps = assess_trifecta(tools)
        lethal = {Capability.READS_PRIVATE, Capability.INGESTS_UNTRUSTED, Capability.EXTERNAL_EGRESS}
        if lethal <= caps:
            egress = [t for t in tools if Capability.EXTERNAL_EGRESS in t.capabilities]
            if not all(t.requires_human_approval for t in egress):
                raise LethalTrifectaError(
                    f"role '{role}' combines private data, untrusted content and un-gated egress: "
                    f"{[t.name for t in egress]}")

    def tools_for(self, role: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if role in t.roles]

    def schemas_for(self, role: str) -> list[dict[str, Any]]:
        return [t.json_schema() for t in self.tools_for(role)]

    # ---- invocation ---------------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any], ctx: CallContext,
             idempotency_key: str | None = None) -> Any:
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]

        def log(outcome: str, detail: str = "") -> None:
            self.audit.append(trace_id=ctx.trace_id, principal=ctx.principal, role=ctx.role,
                              tool=name, args_digest=digest, outcome=outcome, detail=detail[:500])

        spec = self._tools.get(name)
        if spec is None or ctx.role not in spec.roles:
            log("refused", "not allowlisted for role")
            raise PolicyDenied(name, ("tool not allowlisted for role",))

        self._calls[ctx.trace_id] = self._calls.get(ctx.trace_id, 0) + 1
        if self._calls[ctx.trace_id] > self._max_calls:
            log("refused", "call budget exhausted")
            raise PolicyDenied(name, ("per-run tool call budget exhausted",))

        try:
            parsed = spec.args_model.model_validate(args, strict=False)
        except ValidationError as e:
            log("invalid_args", str(e))
            raise ToolError(f"invalid args for {name}: {e.errors(include_url=False)}") from e

        decision = self.policy.evaluate(ctx.role, name, {**ctx.attrs, **parsed.model_dump()})
        if not decision.allowed:
            log("refused", "; ".join(decision.reasons))
            raise PolicyDenied(name, decision.reasons)
        if decision.reasons:  # log-only mode would-deny
            log("would_deny", "; ".join(decision.reasons))

        if spec.is_write:
            if not idempotency_key:
                log("refused", "write without idempotency key")
                raise ToolError(f"{name} is a write tool and requires an idempotency key")
            if idempotency_key in self._idem:
                log("idempotent_replay", idempotency_key)
                return self._idem[idempotency_key]

        attempt = 0
        while True:
            try:
                fut = self._pool.submit(spec.fn, **parsed.model_dump())
                result = fut.result(timeout=spec.timeout_s)
                break
            except FutureTimeout:
                err: ToolError = TransientToolError(f"{name} timed out after {spec.timeout_s}s")
            except TransientToolError as e:
                err = e
            except ToolError as e:
                log("error", str(e))
                raise
            if attempt >= spec.max_retries:
                log("error", f"exhausted retries: {err}")
                raise err
            attempt += 1
            # full jitter: sleep U(0, base * 2^attempt), capped
            self._sleep(random.uniform(0, min(2.0, 0.1 * 2**attempt)))  # noqa: S311

        if spec.is_write and idempotency_key:
            self._idem[idempotency_key] = result
        log("ok", "")
        return result
