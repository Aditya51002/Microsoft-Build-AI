"""REST API routes for ResearchSwarm."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.orchestrator import Orchestrator
from core.task_dag import TaskDAG
from core.types import AgentType, TaskStatus
from core.schemas import ResearchQuery

router = APIRouter()


class CreateSessionRequest(BaseModel):
    """Request body for creating a new research session."""

    query: str = Field(..., min_length=10, max_length=500, description="User query")


class SessionCreateResponse(BaseModel):
    """Response body for session creation."""

    session_id: str
    status: str
    estimated_time_seconds: int


class SessionStatusResponse(BaseModel):
    """Response body for session status queries."""

    session_id: str
    status: str
    dag_summary: Dict[str, Any]
    agent_states: Dict[str, Dict[str, int]]
    created_at: Optional[str]
    elapsed_seconds: Optional[float]


class ReportResponse(BaseModel):
    """Response body for session reports."""

    session_id: str
    report: str
    sources: List[str]
    confidence: float
    critic_notes: List[str]
    retry_questions: List[str]
    claim_ledger: List[Dict[str, Any]]


class CancelResponse(BaseModel):
    """Response body for session cancellation."""

    cancelled: bool


class HealthResponse(BaseModel):
    """Response body for health checks."""

    status: str
    redis: str
    agents: str


def _get_orchestrator(request: Request) -> Orchestrator:
    """Fetch orchestrator instance from app state."""

    return request.app.state.orchestrator


@router.post("/api/sessions", response_model=SessionCreateResponse)
async def create_session(request: Request, body: CreateSessionRequest) -> SessionCreateResponse:
    """Create a new research session."""

    orchestrator = _get_orchestrator(request)
    session_id = str(uuid4())
    query = ResearchQuery(
        user_query=body.query,
        session_id=session_id,
    )

    session_id = await orchestrator.start_session(query)

    created_at = datetime.now(timezone.utc).isoformat()
    await request.app.state.redis.hset(
        f"session:{session_id}:meta",
        mapping={"created_at": created_at, "query": body.query},
    )

    return SessionCreateResponse(
        session_id=session_id,
        status="started",
        estimated_time_seconds=90,
    )


@router.get("/api/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session_status(request: Request, session_id: str) -> SessionStatusResponse:
    """Fetch current session status and DAG summary."""

    orchestrator = _get_orchestrator(request)
    summary = await orchestrator.get_session_status(session_id)
    if summary.get("status") == "unknown":
        raise HTTPException(status_code=404, detail="Session not found")

    created_at = await request.app.state.redis.hget(
        f"session:{session_id}:meta", "created_at"
    )
    elapsed = None
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at)
            elapsed = (datetime.now(timezone.utc) - created_dt).total_seconds()
        except ValueError:
            elapsed = None

    agent_states: Dict[str, Dict[str, int]] = {}
    for task in summary.get("tasks", []):
        agent = task.get("agent_type")
        status_value = task.get("status")
        if not agent or not status_value:
            continue
        agent_states.setdefault(agent, {})
        agent_states[agent][status_value] = agent_states[agent].get(status_value, 0) + 1

    status_value = "done" if summary.get("complete") else "running"

    return SessionStatusResponse(
        session_id=session_id,
        status=status_value,
        dag_summary=summary,
        agent_states=agent_states,
        created_at=created_at,
        elapsed_seconds=elapsed,
    )


@router.get("/api/sessions/{session_id}/report", response_model=ReportResponse)
async def get_session_report(request: Request, session_id: str) -> ReportResponse:
    """Return the final report for a session if available."""

    raw_dag = await request.app.state.redis.get(f"session:{session_id}:dag")
    if not raw_dag:
        raise HTTPException(status_code=404, detail="Session not found")

    dag = TaskDAG.from_json(raw_dag)
    report_text = None
    sources: List[str] = []
    confidence = 0.0
    critic_notes: List[str] = []
    retry_questions: List[str] = []
    claim_ledger: List[Dict[str, Any]] = []

    for node in dag._nodes.values():
        if node.task.to_agent == AgentType.RESEARCHER and node.result:
            claim_ledger.extend(_extract_claims(node.result))

        if node.task.to_agent == AgentType.CRITIC and node.result:
            critique = _parse_result_content(node.result)
            critic_notes = [
                item for item in critique.get("critique_notes", []) if isinstance(item, str)
            ]
            retry_questions = [
                item for item in critique.get("retry_questions", []) if isinstance(item, str)
            ]
            raw_confidence = critique.get("final_confidence", node.result.get("confidence", 0.0))
            if isinstance(raw_confidence, (int, float)):
                confidence = float(raw_confidence)

        if node.task.to_agent != AgentType.WRITER:
            continue
        if node.status != TaskStatus.DONE:
            continue
        if not node.result:
            continue
        content = node.result.get("content")
        sources = node.result.get("sources", []) or []
        try:
            parsed = json.loads(content) if isinstance(content, str) else {}
            report_text = parsed.get("report")
        except json.JSONDecodeError:
            report_text = None
        if isinstance(node.result.get("confidence"), (int, float)):
            confidence = float(node.result.get("confidence"))

    if not report_text:
        raise HTTPException(status_code=404, detail="Report not ready")

    return ReportResponse(
        session_id=session_id,
        report=report_text,
        sources=sources,
        confidence=confidence,
        critic_notes=critic_notes,
        retry_questions=retry_questions,
        claim_ledger=claim_ledger,
    )


@router.delete("/api/sessions/{session_id}", response_model=CancelResponse)
async def cancel_session(request: Request, session_id: str) -> CancelResponse:
    """Cancel a running session."""

    orchestrator = _get_orchestrator(request)
    await orchestrator.cancel_session(session_id)
    return CancelResponse(cancelled=True)


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Basic health check endpoint."""

    redis_client = request.app.state.redis
    try:
        await redis_client.ping()
        redis_status = "connected"
    except Exception:
        redis_status = "unavailable"

    orchestrator = _get_orchestrator(request)
    agents_running = any(
        task for task in orchestrator._agent_tasks if not task.done()
    )

    return HealthResponse(
        status="ok",
        redis=redis_status,
        agents="running" if agents_running else "idle",
    )


def _parse_result_content(result: Dict[str, Any]) -> Dict[str, Any]:
    """Parse an AgentResult content payload into a dictionary."""

    content = result.get("content")
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_claims(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract source-backed claims from a researcher result."""

    parsed = _parse_result_content(result)
    findings = parsed.get("findings", [])
    if not isinstance(findings, list):
        return []

    claims: List[Dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        fact = finding.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            continue
        confidence = finding.get("confidence", result.get("confidence", 0.0))
        claims.append(
            {
                "claim": fact.strip(),
                "source": finding.get("source") or "",
                "confidence": float(confidence)
                if isinstance(confidence, (int, float))
                else 0.0,
                "task_id": result.get("task_id"),
            }
        )
    return claims
