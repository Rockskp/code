"""Entrypoints: `uvicorn appetite.app:api`, `python -m appetite.app mcp`, or AgentCore Runtime."""

from __future__ import annotations

import os
import sys

from agentkit.service import agentcore_entrypoint, create_app

from .agent import build_retriever, build_runner
from .policies import AGENT, SENIOR, UW


def _checkpointer():
    """Postgres in deployed envs (durable across workers); in-memory locally."""
    url = os.getenv("CHECKPOINT_DATABASE_URL")
    if not url:
        return None
    from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore[import-not-found]

    saver = PostgresSaver.from_conn_string(url).__enter__()
    saver.setup()
    return saver


runner, gateway, world = build_runner(checkpointer=_checkpointer())
api = create_app(runner, "Appetite underwriting triage", reviewer_roles={UW, SENIOR})

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "agentcore"
    if mode == "mcp":
        from agentkit.mcp_server import build_mcp_server

        build_mcp_server("appetite-context", gateway, build_retriever(), AGENT).run("stdio")
    else:
        agentcore_entrypoint(runner).run()
