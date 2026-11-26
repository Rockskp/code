# Sentinel: agentic AI for Finance (retail/commercial banking)

> Portfolio project. **All data is synthetic.** Status: core agent VALIDATED by tests (offline); AWS deploy DRAFT.

## Problem and outcome
L1 AML transaction-monitoring alert triage. Banks close 90%+ of alerts as false positives by hand; Sentinel disposes each alert as CLOSE / ESCALATE / SAR with cited manual sections, and only a BSA Officer can approve a SAR.

- Outcomes: CLOSE (agent), ESCALATE (agent), SAR (BSA Officer only)
- Human roles: aml_agent, aml_analyst, bsa_officer
- Release gate (critical miss): expected SAR/ESCALATE but CLOSE (releases laundering risk) must be 0%.
- Baseline (rules model, offline): 91.7% accuracy, 0 critical misses, 100% injection resilience (24 cases). Misses F10/F17 need reading KYC notes: the LLM's job.

## Architecture
```mermaid
flowchart LR
  subgraph Client
    UI[Reviewer UI / case mgmt] -->|OIDC JWT| APIGW[API Gateway]
  end
  APIGW --> API[FastAPI service]
  API -->|invoke run/resume| RT[AgentCore Runtime: LangGraph]
  subgraph Graph[Durable LangGraph workflow]
    I[intake: redact + injection scan + spotlight] --> V[investigate] --> R[retrieve: agentic RAG] --> P[propose: Claude structured output]
    P --> G[guard: citations, hard rules, policy pre-check]
    G -->|auto| C[commit idempotent]
    G -->|human| H[human_review interrupt] --> C
  end
  RT --- Graph
  Graph -->|MCP| GW[AgentCore Gateway + Cedar Policy]
  GW --> T[Tools / context layer MCP server]
  T --> SOR[(System of record)]
  R --> KB[(Policy KB: BM25 local / Bedrock KB hybrid)]
  Graph --> CK[(Postgres checkpoints)]
  Graph --> MEM[(AgentCore Memory)]
  Graph --> OBS[Langfuse + CloudWatch/OTel]
  GW --> AUD[(Hash-chained audit -> S3 Object Lock)]
```

Failure paths: model refusal / budget exceeded / schema failure -> NEEDS_HUMAN (fail safe, never open). Tool transient error -> bounded retry with jitter. Worker crash -> resume same thread_id from last checkpoint; commit is idempotent (exactly-once effect). Invalid human sign-off -> rejected before resume, and the node re-interrupts if bypassed.

## Skills applied (course map)
| Skill | Where |
|---|---|
| ADLC / Claude Code | `AGENTS.md`, spec-first, tests as contract |
| Harness engineering | `agentkit/decision_graph.py` fixed workflow; LLM only proposes |
| Model selection | `agentkit/models.py` catalog + `RoutingRule`, env overrides, heuristic baseline floor |
| Evals + Langfuse | `evals/golden.jsonl`, `evals/run_evals.py`, `agentkit/evals.py` (critical-miss gate, LLM judge, Langfuse push) |
| Agentic RAG over MCP | `agentkit/retrieval.py` multi-query evidence + sufficiency; `agentkit/mcp_server.py` (`search_policy`) |
| Durable state + HITL | LangGraph checkpointer + `interrupt()` / `Command(resume=)` |
| Guardrails / lethal trifecta | `agentkit/guardrails.py`, trifecta check in `agentkit/gateway.py` |
| AI gateway / policy | `agentkit/gateway.py` + `policies/*.cedar` (parity tested) |
| AgentCore deploy | `agentkit/service.py` entrypoint, `deploy/` |

Tools: get_alert, get_customer, get_transactions, screen_sanctions, risk_signals, record_decision

## Key decisions (ADRs)
1. **Fixed graph, LLM proposes only** - arithmetic, thresholds and writes are code; cheaper, testable, auditable. Rejected: free ReAct loop (non-deterministic side effects).
2. **Defence in depth on authority** - Cedar at AgentCore Gateway + same rules in-app + graph guard + reviewer-role validation. Rejected: prompt-only rules.
3. **Model**: claude-opus-5-5 (effort medium) for judgement; heuristic baseline offline. Re-decided only on eval evidence (accuracy, critical miss, cost/case).
4. **BM25 locally, hybrid in AWS** - policy manuals are term-heavy (codes, thresholds); add dense + rerank when evals show recall gaps.
5. **Postgres checkpointer** for durable state across workers; AgentCore Memory only for long-term memory.

## Security and compliance
BSA/AML (31 CFR 1020.320 SAR), FinCEN 314, OFAC sanctions, SOX ITGC, PCI-DSS (PAN redaction, Luhn-gated), GLBA, SR 11-7 model risk management.
- Prompt injection: heuristic scan + nonce-bound spotlighting + no instruction-following rule; suspicious text forces human review. Authority never depends on the model obeying.
- PII/PHI: tokenizing redaction before prompts, logs and checkpoints (raw untrusted text dropped from state). Production adds Comprehend Medical / Presidio NER.
- Lethal trifecta: gateway refuses any role combining private data + untrusted content + un-gated egress.
- AuthN at API Gateway (OIDC); AuthZ re-checked in graph and gateway. Audit log hash-chained (tamper-evident).
- Status: controls VALIDATED by unit tests; SOC 2 / regulatory compliance NOT claimed (needs evidence collection).

## Cost estimate (illustrative, 50k alerts/day)
Assume ~6k input + 800 output tokens/case. First-party list prices (Nov 2026): Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per MTok; **Bedrock pricing differs - confirm.**
- Opus 5.5: ~$0.040/case; Sonnet 5.5: ~$0.020/case; Haiku 5.5: ~$0.001/case.
- FinOps: prompt caching of system+policy prefix (largest lever), route clear-cut cases to heuristic/Haiku, Batch for non-urgent backlog, eval-gated model downgrades.
- Infra (AgentCore Runtime, Aurora Serverless v2, Gateway, CloudWatch): roughly $1.5k-4k/month at this volume; dominated by tokens.

## Run (Windows/VS Code, no Docker)
```powershell
uv venv; uv pip install -e ../../platform/agentkit -e ".[dev]"
uv run pytest -q
uv run python evals/run_evals.py            # offline baseline
$env:AGENT_LLM_MODE="live"; $env:AWS_REGION="us-east-1"; uv run python evals/run_evals.py   # Claude on Bedrock
$env:AGENT_API_KEY="dev-key"; uv run uvicorn sentinel.app:api --reload
uv run python -m sentinel.app mcp         # MCP context-layer server (stdio)
```

## Deploy (DRAFT, AWS Bedrock AgentCore)
See `deploy/README.md`. Build in the cloud (CodeBuild) - no local Docker. `agentcore deploy` with `sentinel.app` entrypoint; Gateway target = MCP server; Cedar policies in `policies/` (log-only first, then enforce). Rollback: redeploy previous runtime version; checkpoints are forward-compatible by additive state keys.

## Next steps
Run live evals on Bedrock and record Langfuse experiment; add LLM-judge grades; validate Cedar schema in a sandbox account; Terraform for Aurora/KMS/S3 Object Lock; red-team set (50+ injection variants).
