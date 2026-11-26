# Deploy Sentinel to AWS Bedrock AgentCore (STATUS: DRAFT, not executed)

1. Prereqs: AWS account with Bedrock model access (Claude), AgentCore enabled, GitHub OIDC role for CI.
2. `pip install bedrock-agentcore-starter-toolkit` (or the AgentCore CLI) and `agentcore configure -e src/sentinel/app.py`.
3. Secrets in Secrets Manager: `AGENT_API_KEY`, `CHECKPOINT_DATABASE_URL` (Aurora PostgreSQL, IAM auth preferred). No secrets in env files.
4. `agentcore deploy` (container built remotely by CodeBuild). Set `AGENT_LLM_MODE=live`, `AWS_REGION`.
5. Gateway: register the MCP server (`python -m sentinel.app mcp`, packaged as Lambda/container) as a target; OAuth inbound via Cognito/Entra.
6. Policy: attach `policies/*.cedar` in LOG_ONLY, compare decisions in CloudWatch for 1 week, then ENFORCE.
7. Memory: create AgentCore Memory; set `MEMORY_BACKEND=agentcore`, namespace `<tenant>/<entity>/<id>`.
8. Promote dev -> staging -> prod with approval; canary 5% via runtime versions; auto-rollback on SLO burn (error rate, p95, critical-miss in shadow eval).
