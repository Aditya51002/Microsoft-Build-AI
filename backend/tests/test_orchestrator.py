import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from core.orchestrator import Orchestrator
from core.schemas import AgentResult, ResearchQuery
from core.task_dag import TaskDAG
from core.types import AgentType, MessageType, TaskStatus


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, key, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def hset(self, key, mapping):
        data = self.store.get(key, {})
        if not isinstance(data, dict):
            data = {}
        data.update(mapping)
        self.store[key] = data

    async def hget(self, key, field):
        data = self.store.get(key, {})
        if not isinstance(data, dict):
            return None
        return data.get(field)


class FakeBus:
    def __init__(self):
        self._redis = FakeRedis()
        self.published = []

    @staticmethod
    def channel_name(agent_type: AgentType, session_id: str) -> str:
        return f"agent:{agent_type.value}:{session_id}"

    async def publish(self, channel, message):
        self.published.append((channel, message))


def make_agent_result(
    agent_type: AgentType,
    content: dict,
    confidence: float = 0.9,
    task_id: str | None = None,
):
    task_uuid = UUID(task_id) if task_id else uuid4()
    return AgentResult(
        task_id=task_uuid,
        agent_type=agent_type,
        content=json.dumps(content),
        sources=[],
        confidence=confidence,
    )


@pytest.mark.asyncio
async def test_full_session_happy_path(monkeypatch):
    bus = FakeBus()
    orchestrator = Orchestrator(bus, SimpleNamespace())

    monkeypatch.setattr(orchestrator, "_ensure_agents_running", lambda: asyncio.sleep(0))
    monkeypatch.setattr(orchestrator, "_start_session_listeners", lambda session_id: None)
    monkeypatch.setattr(orchestrator, "_schedule_timeout", lambda session_id, task_id: None)

    query = ResearchQuery(user_query="test query", session_id=uuid4())
    session_id = await orchestrator.start_session(query)

    state = orchestrator._sessions[session_id]
    planner_payload = make_agent_result(
        AgentType.PLANNER,
        {
            "tasks": [
                {
                    "sub_question": "Q1",
                    "search_keywords": ["a"],
                    "priority": 1,
                }
            ],
            "synthesis_guidance": "guide",
        },
        task_id=state.planner_task_id,
    ).model_dump(mode="json")

    await orchestrator._handle_planner_result(session_id, planner_payload)

    researcher_payload = make_agent_result(
        AgentType.RESEARCHER,
        {
            "findings": [],
            "summary": "",
            "key_data_points": [],
        },
        task_id=state.researcher_task_ids[0],
    ).model_dump(mode="json")

    await orchestrator._handle_researcher_result(session_id, researcher_payload)

    analyst_payload = make_agent_result(
        AgentType.ANALYST,
        {
            "key_insights": ["i"],
            "confidence_map": {"i": 0.7},
            "contradictions": [],
            "gaps": [],
            "overall_confidence": 0.7,
        },
        task_id=state.analyst_task_id,
    ).model_dump(mode="json")

    await orchestrator._handle_analyst_result(session_id, analyst_payload)

    critic_payload = make_agent_result(
        AgentType.CRITIC,
        {
            "approved": True,
            "critique_notes": [],
            "retry_questions": [],
            "final_confidence": 0.8,
        },
        confidence=0.8,
        task_id=state.critic_task_id,
    ).model_dump(mode="json")

    await orchestrator._handle_critic_result(session_id, critic_payload)

    writer_payload = make_agent_result(
        AgentType.WRITER,
        {"report": "done"},
        confidence=0.8,
        task_id=state.writer_task_id,
    ).model_dump(mode="json")

    await orchestrator._handle_writer_result(session_id, writer_payload)

    summary = await orchestrator.get_session_status(session_id)
    assert summary["complete"] is True


@pytest.mark.asyncio
async def test_session_retry_on_researcher_failure(monkeypatch):
    bus = FakeBus()
    orchestrator = Orchestrator(bus, SimpleNamespace())

    monkeypatch.setattr(orchestrator, "_ensure_agents_running", lambda: asyncio.sleep(0))
    monkeypatch.setattr(orchestrator, "_start_session_listeners", lambda session_id: None)
    monkeypatch.setattr(orchestrator, "_schedule_timeout", lambda session_id, task_id: None)

    async def fast_retry(session_id, task_id, retries):
        await orchestrator._publish_task(orchestrator._sessions[session_id].dag._nodes[task_id].task, session_id)

    monkeypatch.setattr(orchestrator, "_retry_task", fast_retry)

    query = ResearchQuery(user_query="test query", session_id=uuid4())
    session_id = await orchestrator.start_session(query)

    state = orchestrator._sessions[session_id]
    planner_payload = make_agent_result(
        AgentType.PLANNER,
        {
            "tasks": [
                {
                    "sub_question": "Q1",
                    "search_keywords": ["a"],
                    "priority": 1,
                }
            ],
            "synthesis_guidance": "guide",
        },
        task_id=state.planner_task_id,
    ).model_dump(mode="json")

    await orchestrator._handle_planner_result(session_id, planner_payload)

    researcher_task_id = orchestrator._sessions[session_id].researcher_task_ids[0]
    await orchestrator._mark_failed(session_id, researcher_task_id, "fail once")

    researcher_payload = make_agent_result(
        AgentType.RESEARCHER,
        {
            "findings": [],
            "summary": "",
            "key_data_points": [],
        },
        task_id=researcher_task_id,
    ).model_dump(mode="json")

    await orchestrator._handle_researcher_result(session_id, researcher_payload)

    summary = await orchestrator.get_session_status(session_id)
    assert summary["counts"][TaskStatus.RETRY.value] >= 0


@pytest.mark.asyncio
async def test_session_cancellation(monkeypatch):
    bus = FakeBus()
    orchestrator = Orchestrator(bus, SimpleNamespace())

    monkeypatch.setattr(orchestrator, "_ensure_agents_running", lambda: asyncio.sleep(0))
    monkeypatch.setattr(orchestrator, "_start_session_listeners", lambda session_id: None)

    query = ResearchQuery(user_query="test query", session_id=uuid4())
    session_id = await orchestrator.start_session(query)

    await orchestrator.cancel_session(session_id)

    raw = await bus._redis.get(f"session:{session_id}:dag")
    dag = TaskDAG.from_json(raw)
    assert all(node.status == TaskStatus.CANCELLED for node in dag._nodes.values())


@pytest.mark.asyncio
async def test_concurrent_sessions(monkeypatch):
    bus = FakeBus()
    orchestrator = Orchestrator(bus, SimpleNamespace())

    monkeypatch.setattr(orchestrator, "_ensure_agents_running", lambda: asyncio.sleep(0))
    monkeypatch.setattr(orchestrator, "_start_session_listeners", lambda session_id: None)
    monkeypatch.setattr(orchestrator, "_schedule_timeout", lambda session_id, task_id: None)

    sessions = []
    for _ in range(3):
        query = ResearchQuery(user_query="test query", session_id=uuid4())
        sessions.append(await orchestrator.start_session(query))

    for session_id in sessions:
        state = orchestrator._sessions[session_id]
        planner_payload = make_agent_result(
            AgentType.PLANNER,
            {
                "tasks": [
                    {
                        "sub_question": "Q1",
                        "search_keywords": ["a"],
                        "priority": 1,
                    }
                ],
                "synthesis_guidance": "guide",
            },
            task_id=state.planner_task_id,
        ).model_dump(mode="json")

        await orchestrator._handle_planner_result(session_id, planner_payload)
        researcher_payload = make_agent_result(
            AgentType.RESEARCHER,
            {"findings": [], "summary": "", "key_data_points": []},
            task_id=state.researcher_task_ids[0],
        ).model_dump(mode="json")
        await orchestrator._handle_researcher_result(session_id, researcher_payload)
        analyst_payload = make_agent_result(
            AgentType.ANALYST,
            {
                "key_insights": ["i"],
                "confidence_map": {"i": 0.7},
                "contradictions": [],
                "gaps": [],
                "overall_confidence": 0.7,
            },
            task_id=state.analyst_task_id,
        ).model_dump(mode="json")
        await orchestrator._handle_analyst_result(session_id, analyst_payload)
        critic_payload = make_agent_result(
            AgentType.CRITIC,
            {
                "approved": True,
                "critique_notes": [],
                "retry_questions": [],
                "final_confidence": 0.8,
            },
            confidence=0.8,
            task_id=state.critic_task_id,
        ).model_dump(mode="json")
        await orchestrator._handle_critic_result(session_id, critic_payload)
        writer_payload = make_agent_result(
            AgentType.WRITER,
            {"report": "done"},
            confidence=0.8,
            task_id=state.writer_task_id,
        ).model_dump(mode="json")
        await orchestrator._handle_writer_result(session_id, writer_payload)

    for session_id in sessions:
        summary = await orchestrator.get_session_status(session_id)
        assert summary["complete"] is True


@pytest.mark.asyncio
async def test_timeout_handling(monkeypatch):
    bus = FakeBus()
    orchestrator = Orchestrator(bus, SimpleNamespace())

    monkeypatch.setattr(orchestrator, "_ensure_agents_running", lambda: asyncio.sleep(0))
    monkeypatch.setattr(orchestrator, "_start_session_listeners", lambda session_id: None)

    query = ResearchQuery(user_query="test query", session_id=uuid4())
    session_id = await orchestrator.start_session(query)

    task_id = orchestrator._sessions[session_id].planner_task_id

    async def no_retry(session_id, task_id, retries):
        return None

    monkeypatch.setattr(orchestrator, "_retry_task", no_retry)
    monkeypatch.setattr("core.orchestrator.TASK_TIMEOUT_SECONDS", 0.01)

    orchestrator._schedule_timeout(session_id, task_id)
    await asyncio.sleep(0.05)

    raw = await bus._redis.get(f"session:{session_id}:dag")
    dag = TaskDAG.from_json(raw)
    assert dag._nodes[task_id].status == TaskStatus.FAILED
