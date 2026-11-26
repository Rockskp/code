# Agentic AI FDE portfolio: Finance, Medical, Insurance

Three production-shaped agents built on one shared platform kit. All data is synthetic.

| Project | Domain | Agent | Offline baseline |
|---|---|---|---|
| [finance-aml-triage](projects/finance-aml-triage) | Banking AML | Sentinel: alert CLOSE/ESCALATE/SAR | 91.7%, 0 critical |
| [healthcare-prior-auth](projects/healthcare-prior-auth) | Health plan UM | PriorPath: APPROVE/PEND/DENY | 90.9%, 0 critical |
| [insurance-underwriting](projects/insurance-underwriting) | Commercial P&C | Appetite: QUOTE/REFER/DECLINE | 95.5%, 0 critical |

`platform/agentkit`: guardrails (injection, spotlighting, PII/PHI redaction), Cedar-style policy engine, tool gateway (allowlist, schema, policy, retries, idempotency, hash-chained audit, lethal-trifecta check), agentic RAG, model catalog/router + cost meter, memory, eval harness (Langfuse), durable LangGraph decide-against-policy graph with HITL, MCP server, FastAPI + AgentCore entrypoint.

Tests: 50 passing (16 platform + 11 + 15 + 8). Python 3.12+, `uv`.
