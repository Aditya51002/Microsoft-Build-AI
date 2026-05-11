import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock

from agents.planner import PlannerAgent
from agents.researcher import ResearcherAgent
from agents.critic import CriticAgent
from agents.writer import WriterAgent
from core.schemas import AgentMessage, AgentResult, TaskMessage
from core.types import AgentType, MessageType, TaskStatus


class FakeBus:
    def __init__(self):
        self.published = []

    @staticmethod
    def channel_name(agent_type: AgentType, session_id: str) -> str:
        return f"agent:{agent_type.value}:{session_id}"

    async def publish(self, channel: str, message):
        self.published.append((channel, message))


class FakeResponse:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(type="text", text=text)]


def make_task(agent: AgentType, payload: dict) -> TaskMessage:
    return TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=agent,
        to_agent=agent,
        payload=payload,
        status=TaskStatus.PENDING,
        confidence=0.9,
        task_id=uuid4(),
        parent_task_id=None,
        depth=0,
    )


@pytest.mark.asyncio
async def test_planner_decomposes_query():
    bus = FakeBus()
    tasks = [
        {
            "id": str(uuid4()),
            "sub_question": "Q1",
            "search_keywords": ["a"],
            "priority": 1,
        },
        {
            "id": str(uuid4()),
            "sub_question": "Q2",
            "search_keywords": ["b"],
            "priority": 2,
        },
        {
            "id": str(uuid4()),
            "sub_question": "Q3",
            "search_keywords": ["c"],
            "priority": 3,
        },
    ]
    response = FakeResponse(json.dumps({"tasks": tasks, "synthesis_guidance": "Guide"}))
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))

    planner = PlannerAgent(bus, anthropic_client)
    task = make_task(AgentType.PLANNER, {"user_query": "test query", "session_id": "s1"})

    planner._demo_mode_enabled = lambda: False
    result = await planner.process(task)

    planned = json.loads(result.content)
    assert 3 <= len(planned["tasks"]) <= 6


@pytest.mark.asyncio
async def test_planner_retries_on_invalid_json():
    bus = FakeBus()
    invalid = FakeResponse("not-json")
    valid = FakeResponse(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": str(uuid4()),
                        "sub_question": "Q1",
                        "search_keywords": ["a"],
                        "priority": 1,
                    },
                    {
                        "id": str(uuid4()),
                        "sub_question": "Q2",
                        "search_keywords": ["b"],
                        "priority": 2,
                    },
                    {
                        "id": str(uuid4()),
                        "sub_question": "Q3",
                        "search_keywords": ["c"],
                        "priority": 3,
                    },
                ],
                "synthesis_guidance": "Guide",
            }
        )
    )
    anthropic_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=[invalid, valid]))
    )

    planner = PlannerAgent(bus, anthropic_client)
    task = make_task(AgentType.PLANNER, {"user_query": "test query", "session_id": "s1"})

    planner._demo_mode_enabled = lambda: False
    result = await planner.process(task)
    assert "tasks" in json.loads(result.content)


@pytest.mark.asyncio
async def test_researcher_handles_empty_search():
    bus = FakeBus()
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    researcher = ResearcherAgent(bus, anthropic_client)

    calls = []

    async def fake_research(sub_question, keywords):
        calls.append(list(keywords))
        if len(calls) == 1:
            return {"findings": [], "summary": "", "key_data_points": []}, "{}", [], 0
        return (
            {
                "findings": [
                    {"fact": "f", "source": "https://example.com", "confidence": 0.7}
                ],
                "summary": "ok",
                "key_data_points": ["p"],
            },
            "{}",
            ["https://example.com"],
            1,
        )

    researcher._research = AsyncMock(side_effect=fake_research)
    researcher._demo_mode_enabled = lambda: False

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.PLANNER,
        to_agent=AgentType.RESEARCHER,
        payload={
            "sub_question": "What is current solar capacity?",
            "search_keywords": ["current solar capacity Vietnam"],
            "session_id": "s1",
        },
        status=TaskStatus.PENDING,
        confidence=0.9,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    await researcher.process(task)

    assert len(calls) == 2
    assert "current" not in calls[1][0].lower()


@pytest.mark.asyncio
async def test_researcher_extracts_sources():
    bus = FakeBus()
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    researcher = ResearcherAgent(bus, anthropic_client)

    async def fake_research(sub_question, keywords):
        return (
            {
                "findings": [
                    {"fact": "f", "source": "https://example.com", "confidence": 0.7}
                ],
                "summary": "ok",
                "key_data_points": ["p"],
            },
            "{}",
            ["https://example.com"],
            1,
        )

    researcher._research = AsyncMock(side_effect=fake_research)
    researcher._demo_mode_enabled = lambda: False

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.PLANNER,
        to_agent=AgentType.RESEARCHER,
        payload={
            "sub_question": "Q",
            "search_keywords": ["k"],
            "session_id": "s1",
        },
        status=TaskStatus.PENDING,
        confidence=0.9,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    result = await researcher.process(task)
    assert "https://example.com" in result.sources


@pytest.mark.asyncio
async def test_critic_requests_retry_on_low_confidence():
    bus = FakeBus()
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    critic = CriticAgent(bus, anthropic_client)

    critic._call_claude = AsyncMock(
        return_value={
            "approved": False,
            "critique_notes": ["gap"],
            "retry_questions": ["Investigate policy incentives"],
            "final_confidence": 0.3,
        }
    )

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.ANALYST,
        to_agent=AgentType.CRITIC,
        payload={"analyst_result": {"sources": []}},
        status=TaskStatus.PENDING,
        confidence=0.4,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    result = await critic.process(task)
    critique = json.loads(result.content)
    assert critique["approved"] is False
    assert critique["retry_questions"] == ["Investigate policy incentives"]


@pytest.mark.asyncio
async def test_critic_approves_high_confidence():
    bus = FakeBus()
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
    critic = CriticAgent(bus, anthropic_client)

    critic._call_claude = AsyncMock(
        return_value={
            "approved": True,
            "critique_notes": ["solid"],
            "retry_questions": [],
            "final_confidence": 0.85,
        }
    )

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.ANALYST,
        to_agent=AgentType.CRITIC,
        payload={"analyst_result": {"sources": ["https://example.com"]}},
        status=TaskStatus.PENDING,
        confidence=0.7,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    result = await critic.process(task)
    await critic.emit_result(result)

    assert any(
        isinstance(message, AgentMessage) and message.to_agent == AgentType.PLANNER
        for _, message in bus.published
    )


@pytest.mark.asyncio
async def test_writer_streams_output():
    bus = FakeBus()
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(stream=AsyncMock()))
    writer = WriterAgent(bus, anthropic_client)

    class FakeStream:
        def __init__(self):
            self._chunks = ["Hello ", "world"]

        async def __aenter__(self):
            async def generator():
                for chunk in self._chunks:
                    yield chunk
                    await asyncio.sleep(0)

            self.text_stream = generator()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get_final_message(self):
            return FakeResponse("Hello world")

    async def passthrough(func):
        return await func()

    writer._call_anthropic = AsyncMock(side_effect=passthrough)
    writer.anthropic_client.messages.stream = AsyncMock(return_value=FakeStream())
    writer._demo_mode_enabled = lambda: False

    analyst_result = AgentResult(
        task_id=uuid4(),
        agent_type=AgentType.ANALYST,
        content=json.dumps({"key_insights": ["ok"], "overall_confidence": 0.7}),
        sources=["https://example.com"],
        confidence=0.7,
    )
    critic_result = {
        "approved": True,
        "critique_notes": [],
        "retry_questions": [],
        "final_confidence": 0.7,
    }

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.CRITIC,
        to_agent=AgentType.WRITER,
        payload={
            "analyst_result": analyst_result.model_dump(mode="json"),
            "critic_result": critic_result,
            "session_id": "s1",
        },
        status=TaskStatus.PENDING,
        confidence=0.7,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    await writer.process(task)

    assert any(
        isinstance(message, AgentMessage)
        and message.type == MessageType.STATUS_UPDATE
        and message.payload.get("chunk")
        for _, message in bus.published
    )
