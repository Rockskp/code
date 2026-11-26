"""HTTP API and AgentCore Runtime entrypoint shared by the three agents.

AuthN: in AWS, API Gateway / ALB validates the OIDC JWT (Cognito or Entra ID) and forwards the
verified `sub` and role claim. Locally, `X-Api-Key` + `X-User` + `X-Role` stand in for that.
AuthZ: is *never* trusted from the header alone for decisions. The graph re-checks the role
against the domain's `reviewer_roles`, and the gateway re-checks it against policy.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from pydantic import BaseModel, Field

from .decision_graph import CaseRunner, ReviewRejected


class Principal(BaseModel):
    user: str
    role: str


class ReviewIn(BaseModel):
    outcome: str
    note: str = Field(min_length=10, max_length=2000)


def create_app(runner: CaseRunner, title: str, reviewer_roles: set[str]) -> Any:
    from fastapi import Depends, FastAPI, Header, HTTPException

    app = FastAPI(title=title, version="0.1.0")

    def principal(x_api_key: str = Header(...), x_user: str = Header(...), x_role: str = Header(...)) -> Principal:
        expected = os.getenv("AGENT_API_KEY", "")
        if not expected or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(401, "invalid api key")
        return Principal(user=x_user, role=x_role)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/cases/{case_id}/run")
    def run(case_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
        return runner.start(case_id)

    @app.get("/cases/{case_id}")
    def get(case_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
        return runner.snapshot(case_id)

    @app.post("/cases/{case_id}/review")
    def review(case_id: str, body: ReviewIn, p: Principal = Depends(principal)) -> dict[str, Any]:
        if p.role not in reviewer_roles:
            raise HTTPException(403, "role may not review cases")
        snap = runner.snapshot(case_id)
        if snap["status"] != "awaiting_human":
            raise HTTPException(409, f"case is {snap['status']}, not awaiting review")
        try:
            return runner.resume(case_id, {"outcome": body.outcome, "note": body.note,
                                           "reviewer": p.user, "role": p.role})
        except ReviewRejected as e:
            raise HTTPException(403, str(e)) from e

    return app


def agentcore_entrypoint(runner: CaseRunner) -> Any:
    """Wrap the runner for AgentCore Runtime. Payload: {"action": "run"|"resume", "case_id", "decision"?}."""
    from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore[import-not-found]

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
        action, case_id = payload.get("action", "run"), str(payload["case_id"])
        if action == "resume":
            return runner.resume(case_id, payload.get("decision"))
        if action == "status":
            return runner.snapshot(case_id)
        return runner.start(case_id)

    return app
