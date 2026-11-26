"""Run the golden set: `python evals/run_evals.py [--min-accuracy 0.85]`.

Offline (default) evaluates the rules baseline. `AGENT_LLM_MODE=live` evaluates Claude; set
LANGFUSE_* to also push scores. Exit code 1 if the release gate fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentkit.evals import EvalCase, load_jsonl, push_to_langfuse, run_eval, write_report

from sentinel.agent import build_runner

HERE = Path(__file__).parent


def critical(expected: str, actual: str) -> bool:
    # Releasing money-laundering risk is the costly error; over-escalation is a cost, not a breach.
    return expected in {"SAR", "ESCALATE"} and actual == "CLOSE"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-accuracy", type=float, default=0.85)
    ap.add_argument("--max-critical", type=float, default=0.0)
    a = ap.parse_args()
    runner, _, _ = build_runner()

    def agent(c: EvalCase) -> dict:
        out = runner.start(c.input["alert_id"])
        return {"outcome": out["proposal"]["outcome"], "human_review": out["status"] == "awaiting_human",
                "citations": out["citations"], "cost_usd": (out["usage"] or {}).get("cost_usd", 0.0)}

    rep = run_eval("sentinel-golden", load_jsonl(HERE / "golden.jsonl"), agent, critical)
    print(write_report(rep, HERE / "reports").read_text())
    push_to_langfuse(rep, "sentinel-golden")
    return 0 if rep.passes(a.min_accuracy, a.max_critical) else 1


if __name__ == "__main__":
    sys.exit(main())
