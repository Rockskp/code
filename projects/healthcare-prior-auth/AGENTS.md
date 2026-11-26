# AGENTS.md - PriorPath

Instructions for coding agents (Claude Code) working in this folder.

- Spec: README.md "Problem and outcome". Do not change outcomes, roles or thresholds without an ADR.
- Never move business logic (thresholds, arithmetic, limits) into prompts; put it in `src/priorpath/tools.py`.
- Any change to `src/priorpath/policies.py` must be mirrored in `policies/*.cedar` (test enforces id parity).
- Data is synthetic only. Never add real names, accounts, PHI, or credentials.
- Definition of done: `pytest -q` green, `python evals/run_evals.py` passes the gate (0 critical misses), README updated.
- Review: a fresh sub-agent that did not write the change reviews the diff before merge.
