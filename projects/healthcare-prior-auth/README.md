# PriorPath: agentic AI for Medical (health plan utilization management)

> Portfolio project. **All data is synthetic.** Status: core agent VALIDATED by tests (offline); AWS deploy DRAFT.

## Problem and outcome
Prior authorization review for lumbar MRI, knee arthroplasty, GLP-1 and polysomnography. PriorPath applies plan criteria, approves or pends, and routes every adverse determination to a Medical Director (CMS-0057-F / state UM law).

- Outcomes: APPROVE (agent), PEND_INFO (agent, never for urgent), DENY (Medical Director only)
- Human roles: pa_agent, um_nurse, medical_director
- Release gate (critical miss): APPROVE when criteria not met (bypasses a clinician) must be 0%.
- Baseline (rules model, offline): 90.9% accuracy, 0 critical misses, 100% injection resilience (22 cases). Misses P05/P22 have facts only in the clinical note.

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

Tools: get_request, get_eligibility, get_clinical_record, check_criteria, record_decision

## Key decisions (ADRs)
1. **Fixed graph, LLM proposes only** - arithmetic, thresholds and writes are code; cheaper, testable, auditable. Rejected: free ReAct loop (non-deterministic side effects).
2. **Defence in depth on authority** - Cedar at AgentCore Gateway + same rules in-app + graph guard + reviewer-role validation. Rejected: prompt-only rules.
3. **Model**: claude-opus-5-5 on Bedrock (PHI); refuses non-BAA provider in live mode. Re-decided only on eval evidence (accuracy, critical miss, cost/case).
4. **BM25 locally, hybrid in AWS** - policy manuals are term-heavy (codes, thresholds); add dense + rerank when evals show recall gaps.
5. **Postgres checkpointer** for durable state across workers; AgentCore Memory only for long-term memory.

## Security and compliance
HIPAA Privacy (minimum necessary) & Security Rule, AWS BAA (Bedrock in-account only, enforced at startup), CMS-0057-F turnaround (72h expedited), URAC/NCQA UM, SOC 2.
- Prompt injection: heuristic scan + nonce-bound spotlighting + no instruction-following rule; suspicious text forces human review. Authority never depends on the model obeying.
- PII/PHI: tokenizing redaction before prompts, logs and checkpoints (raw untrusted text dropped from state). Production adds Comprehend Medical / Presidio NER.
- Lethal trifecta: gateway refuses any role combining private data + untrusted content + un-gated egress.
- AuthN at API Gateway (OIDC); AuthZ re-checked in graph and gateway. Audit log hash-chained (tamper-evident).
- Status: controls VALIDATED by unit tests; SOC 2 / regulatory compliance NOT claimed (needs evidence collection).

## Cost estimate (illustrative, 20k requests/day)
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
$env:AGENT_API_KEY="dev-key"; uv run uvicorn priorpath.app:api --reload
uv run python -m priorpath.app mcp         # MCP context-layer server (stdio)
```

## Deploy (DRAFT, AWS Bedrock AgentCore)
See `deploy/README.md`. Build in the cloud (CodeBuild) - no local Docker. `agentcore deploy` with `priorpath.app` entrypoint; Gateway target = MCP server; Cedar policies in `policies/` (log-only first, then enforce). Rollback: redeploy previous runtime version; checkpoints are forward-compatible by additive state keys.

## Next steps
Run live evals on Bedrock and record Langfuse experiment; add LLM-judge grades; validate Cedar schema in a sandbox account; Terraform for Aurora/KMS/S3 Object Lock; red-team set (50+ injection variants).
