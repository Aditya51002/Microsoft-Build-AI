"""Orchestrator for coordinating ResearchSwarm sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from anthropic import AsyncAnthropic

from agents.analyst import AnalystAgent
from agents.critic import CriticAgent
from agents.planner import PlannerAgent
from agents.researcher import ResearcherAgent
from agents.writer import WriterAgent
from core.message_bus import MessageBus
from core.schemas import AgentMessage, AgentResult, ResearchQuery, TaskMessage
from core.task_dag import TaskDAG
from core.types import AgentType, MessageType, TaskStatus

SESSION_DAG_KEY = "session:{session_id}:dag"
WEBSOCKET_CHANNEL_TEMPLATE = "ws:broadcast:{session_id}"
MAX_RESEARCHERS = 1
MAX_RETRIES = 2
TASK_TIMEOUT_SECONDS = 45


@dataclass
class SessionState:
    """Holds orchestration state for an active session."""

    dag: TaskDAG
    planner_task_id: str
    analyst_task_id: Optional[str] = None
    critic_task_id: Optional[str] = None
    writer_task_id: Optional[str] = None
    researcher_task_ids: List[str] = field(default_factory=list)
    analyst_result: Optional[Dict[str, Any]] = None
    critic_result: Optional[Dict[str, Any]] = None
    listeners: List[asyncio.Task] = field(default_factory=list)
    timeouts: Dict[str, asyncio.Task] = field(default_factory=dict)


class Orchestrator:
    """Coordinates agents, task DAG execution, and session state."""

    def __init__(self, message_bus: MessageBus, anthropic_client: AsyncAnthropic) -> None:
        """Initialize orchestrator and spawn agent run loops."""

        self._logger = logging.getLogger("researchswarm.orchestrator")
        self.message_bus = message_bus
        self.anthropic_client = anthropic_client
        self._sessions: Dict[str, SessionState] = {}
        self._agent_tasks: List[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()
        self._signal_handlers_registered = False
        self._shutdown_task: Optional[asyncio.Task] = None

    async def start_session(self, query: ResearchQuery) -> str:
        """Start a new research session and return its session id."""

        self._ensure_signal_handlers()
        await self._ensure_agents_running()

        session_id = str(query.session_id or uuid4())
        dag = TaskDAG(session_id)

        planner_task = TaskMessage(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentType.PLANNER,
            to_agent=AgentType.PLANNER,
            payload={"user_query": query.user_query, "session_id": session_id},
            status=TaskStatus.PENDING,
            confidence=0.9,
            task_id=uuid4(),
            parent_task_id=None,
            depth=0,
        )

        dag.add_task(planner_task)
        state = SessionState(dag=dag, planner_task_id=str(planner_task.task_id))
        self._sessions[session_id] = state

        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

        self._start_session_listeners(session_id)

        await self._publish_task(planner_task, session_id)
        return session_id

    async def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """Retrieve session status summary from Redis."""

        data = await self.message_bus._redis.get(
            SESSION_DAG_KEY.format(session_id=session_id)
        )
        if not data:
            return {"session_id": session_id, "status": "unknown"}
        dag = TaskDAG.from_json(data)
        return dag.get_status_summary()

    async def cancel_session(self, session_id: str) -> None:
        """Cancel all tasks and listeners for a session."""

        state = self._sessions.get(session_id)
        if not state:
            return

        for task_id in list(state.dag._nodes.keys()):
            state.dag.set_status(task_id, TaskStatus.CANCELLED)

        for task in state.listeners:
            task.cancel()
        for timeout in state.timeouts.values():
            timeout.cancel()

        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)
        self._sessions.pop(session_id, None)

    async def shutdown(self) -> None:
        """Shutdown all orchestrator-managed tasks cleanly."""

        self._shutdown_event.set()
        for state in list(self._sessions.values()):
            await self.cancel_session(state.dag.session_id)
        for task in self._agent_tasks:
            task.cancel()

    async def _ensure_agents_running(self) -> None:
        """Start agent run loops if they are not already running."""

        if self._agent_tasks:
            return

        agents = [
            PlannerAgent(self.message_bus, self.anthropic_client),
            AnalystAgent(self.message_bus, self.anthropic_client),
            CriticAgent(self.message_bus, self.anthropic_client),
            WriterAgent(self.message_bus, self.anthropic_client),
        ]
        for _ in range(MAX_RESEARCHERS):
            agents.append(ResearcherAgent(self.message_bus, self.anthropic_client))

        self._agent_tasks = [asyncio.create_task(agent.run()) for agent in agents]

    def _ensure_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""

        if self._signal_handlers_registered:
            return

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, self._shutdown_event.set)
            loop.add_signal_handler(signal.SIGINT, self._shutdown_event.set)
        except NotImplementedError:
            pass
        self._signal_handlers_registered = True

    async def _wait_for_shutdown(self) -> None:
        """Wait for a shutdown signal and stop all running tasks."""

        await self._shutdown_event.wait()
        await self.shutdown()

    def _start_session_listeners(self, session_id: str) -> None:
        """Start pub/sub listeners for a session."""

        state = self._sessions[session_id]
        channel = self.message_bus.channel_name(AgentType.PLANNER, session_id)
        listener = asyncio.create_task(self._listen_channel(session_id, channel))
        state.listeners.append(listener)

    async def _listen_channel(self, session_id: str, channel: str) -> None:
        """Listen to a Redis channel and handle agent messages."""

        try:
            async for message in self.message_bus.subscribe(channel):
                await self._handle_message(session_id, message)
        except asyncio.CancelledError:
            return

    async def _handle_message(self, session_id: str, message: AgentMessage) -> None:
        """Dispatch agent messages to session handlers."""

        if session_id not in self._sessions:
            return

        if message.type == MessageType.ERROR:
            task_id = message.payload.get("task_id")
            if isinstance(task_id, str):
                await self._mark_failed(session_id, task_id, message.payload.get("error"))
            return

        if message.type != MessageType.TASK_RESULT:
            return

        from_agent = message.from_agent
        payload = message.payload if isinstance(message.payload, dict) else {}
        if from_agent == AgentType.PLANNER:
            await self._handle_planner_result(session_id, payload)
        elif from_agent == AgentType.RESEARCHER:
            await self._handle_researcher_result(session_id, payload)
        elif from_agent == AgentType.ANALYST:
            await self._handle_analyst_result(session_id, payload)
        elif from_agent == AgentType.CRITIC:
            await self._handle_critic_result(session_id, payload)
        elif from_agent == AgentType.WRITER:
            await self._handle_writer_result(session_id, payload)

    async def _handle_planner_result(self, session_id: str, payload: Dict[str, Any]) -> None:
        """Create researcher tasks based on planner output."""

        state = self._sessions[session_id]
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            result = AgentResult.model_validate(payload)
            state.dag.mark_done(task_id, result)
            await self._clear_timeout(session_id, task_id)

        content = payload.get("content")
        try:
            plan = json.loads(content) if isinstance(content, str) else {}
        except json.JSONDecodeError:
            plan = {}

        tasks = plan.get("tasks", [])
        researcher_ids: List[str] = []
        for item in tasks:
            task_uuid = uuid4()
            sub_question = item.get("sub_question")
            search_keywords = item.get("search_keywords", [])
            priority = item.get("priority", 1)

            task = TaskMessage(
                type=MessageType.TASK_ASSIGN,
                from_agent=AgentType.PLANNER,
                to_agent=AgentType.RESEARCHER,
                payload={
                    "sub_question": sub_question,
                    "search_keywords": search_keywords,
                    "priority": priority,
                    "session_id": session_id,
                },
                status=TaskStatus.PENDING,
                confidence=0.9,
                task_id=task_uuid,
                parent_task_id=UUID(state.planner_task_id),
                depth=1,
            )
            state.dag.add_task(task, depends_on=[state.planner_task_id])
            researcher_ids.append(str(task_uuid))
            await self._publish_task(task, session_id)

        state.researcher_task_ids.extend(researcher_ids)
        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

    async def _handle_researcher_result(self, session_id: str, payload: Dict[str, Any]) -> None:
        """Handle a researcher result and advance the DAG."""

        state = self._sessions[session_id]
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            return

        result = AgentResult.model_validate(payload)
        state.dag.mark_done(task_id, result)
        await self._clear_timeout(session_id, task_id)
        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

        if state.analyst_task_id:
            return

        if all(
            state.dag._nodes[task].status == TaskStatus.DONE
            for task in state.researcher_task_ids
            if task in state.dag._nodes
        ):
            await self._trigger_analyst(session_id)

    async def _handle_analyst_result(self, session_id: str, payload: Dict[str, Any]) -> None:
        """Handle analyst output and trigger critic."""

        state = self._sessions[session_id]
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            result = AgentResult.model_validate(payload)
            state.analyst_result = payload
            state.dag.mark_done(task_id, result)
            await self._clear_timeout(session_id, task_id)

        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

        await self._trigger_critic(session_id)

    async def _handle_critic_result(self, session_id: str, payload: Dict[str, Any]) -> None:
        """Handle critic output and decide on retry or writing."""

        state = self._sessions[session_id]
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            result = AgentResult.model_validate(payload)
            state.critic_result = payload
            state.dag.mark_done(task_id, result)
            await self._clear_timeout(session_id, task_id)

        critique = {}
        try:
            critique = json.loads(payload.get("content", ""))
        except json.JSONDecodeError:
            critique = {}

        approved = critique.get("approved")
        final_confidence = critique.get("final_confidence", 0.0)
        retry_questions = critique.get("retry_questions") or []

        if not approved or float(final_confidence) < 0.5:
            if retry_questions:
                await self._requeue_research(session_id, retry_questions)
                return
            await self._trigger_writer(session_id)
            return

        await self._trigger_writer(session_id)

    async def _handle_writer_result(self, session_id: str, payload: Dict[str, Any]) -> None:
        """Handle writer output and mark session complete."""

        state = self._sessions[session_id]
        task_id = payload.get("task_id")
        if isinstance(task_id, str):
            result = AgentResult.model_validate(payload)
            state.dag.mark_done(task_id, result)
            await self._clear_timeout(session_id, task_id)

        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

    async def _trigger_analyst(self, session_id: str) -> None:
        """Send a task message to the analyst once researchers complete."""

        state = self._sessions[session_id]
        analyst_task = TaskMessage(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentType.PLANNER,
            to_agent=AgentType.ANALYST,
            payload={
                "expected_results": len(state.researcher_task_ids),
                "researcher_results": self._collect_results(state.researcher_task_ids, state),
                "session_id": session_id,
            },
            status=TaskStatus.PENDING,
            confidence=0.9,
            task_id=uuid4(),
            parent_task_id=UUID(state.planner_task_id),
            depth=2,
        )
        state.analyst_task_id = str(analyst_task.task_id)
        state.dag.add_task(analyst_task, depends_on=state.researcher_task_ids)
        await self._publish_task(analyst_task, session_id)

    def _collect_results(
        self, task_ids: List[str], state: SessionState
    ) -> List[Dict[str, Any]]:
        """Collect completed task results from the DAG for downstream agents."""

        results: List[Dict[str, Any]] = []
        for task_id in task_ids:
            node = state.dag._nodes.get(task_id)
            if not node or not node.result:
                continue
            results.append(node.result)
        return results

    async def _trigger_critic(self, session_id: str) -> None:
        """Send a task message to the critic based on analyst output."""

        state = self._sessions[session_id]
        if not state.analyst_result:
            return

        critic_task = TaskMessage(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentType.ANALYST,
            to_agent=AgentType.CRITIC,
            payload={"analyst_result": state.analyst_result, "session_id": session_id},
            status=TaskStatus.PENDING,
            confidence=0.8,
            task_id=uuid4(),
            parent_task_id=UUID(state.analyst_task_id) if state.analyst_task_id else None,
            depth=3,
        )
        state.critic_task_id = str(critic_task.task_id)
        state.dag.add_task(critic_task, depends_on=[state.analyst_task_id or ""])
        await self._publish_task(critic_task, session_id)

    async def _trigger_writer(self, session_id: str) -> None:
        """Send a task message to the writer with analyst and critic outputs."""

        state = self._sessions[session_id]
        if not state.analyst_result or not state.critic_result:
            return

        writer_task = TaskMessage(
            type=MessageType.TASK_ASSIGN,
            from_agent=AgentType.CRITIC,
            to_agent=AgentType.WRITER,
            payload={
                "analyst_result": state.analyst_result,
                "critic_result": state.critic_result,
                "session_id": session_id,
            },
            status=TaskStatus.PENDING,
            confidence=0.8,
            task_id=uuid4(),
            parent_task_id=UUID(state.critic_task_id) if state.critic_task_id else None,
            depth=4,
        )
        state.writer_task_id = str(writer_task.task_id)
        state.dag.add_task(writer_task, depends_on=[state.critic_task_id or ""])
        await self._publish_task(writer_task, session_id)

    async def _requeue_research(self, session_id: str, retry_questions: List[str]) -> None:
        """Requeue researcher tasks for critic-requested gaps."""

        state = self._sessions[session_id]
        if not retry_questions:
            return

        for question in retry_questions:
            if not isinstance(question, str) or not question.strip():
                continue
            task = TaskMessage(
                type=MessageType.TASK_ASSIGN,
                from_agent=AgentType.CRITIC,
                to_agent=AgentType.RESEARCHER,
                payload={
                    "sub_question": question.strip(),
                    "search_keywords": [question.strip()],
                    "session_id": session_id,
                    "retry": True,
                },
                status=TaskStatus.RETRY,
                confidence=0.3,
                task_id=uuid4(),
                parent_task_id=UUID(state.planner_task_id),
                depth=1,
            )
            state.dag.add_task(task, depends_on=[state.planner_task_id])
            state.researcher_task_ids.append(str(task.task_id))
            await self._publish_task(task, session_id)

        state.analyst_task_id = None
        state.critic_task_id = None
        state.writer_task_id = None
        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

    async def _publish_task(self, task: TaskMessage, session_id: str) -> None:
        """Publish a task to the appropriate agent channel with timeout tracking."""

        channel = self.message_bus.channel_name(task.to_agent, session_id)
        await self.message_bus.publish(channel, task)
        self._schedule_timeout(session_id, str(task.task_id))
        await self._update_task_status(session_id, str(task.task_id), TaskStatus.RUNNING)

    def _schedule_timeout(self, session_id: str, task_id: str) -> None:
        """Schedule a timeout for a task execution."""

        state = self._sessions.get(session_id)
        if not state:
            return
        if task_id in state.timeouts:
            state.timeouts[task_id].cancel()

        async def _timeout() -> None:
            await asyncio.sleep(TASK_TIMEOUT_SECONDS)
            await self._mark_failed(session_id, task_id, "Task timed out")

        state.timeouts[task_id] = asyncio.create_task(_timeout())

    async def _clear_timeout(self, session_id: str, task_id: str) -> None:
        """Clear timeout tracking for a task."""

        state = self._sessions.get(session_id)
        if not state:
            return
        timeout = state.timeouts.pop(task_id, None)
        if timeout:
            timeout.cancel()

    async def _mark_failed(self, session_id: str, task_id: str, error: str) -> None:
        """Mark a task failed and trigger retry logic if available."""

        state = self._sessions.get(session_id)
        if not state:
            return
        state.dag.mark_failed(task_id, str(error))
        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

        retries = state.dag.increment_retry(task_id)
        if retries <= MAX_RETRIES:
            await self._retry_task(session_id, task_id, retries)

    async def _retry_task(self, session_id: str, task_id: str, retries: int) -> None:
        """Requeue a failed task with exponential backoff."""

        state = self._sessions.get(session_id)
        if not state:
            return
        node = state.dag._nodes.get(task_id)
        if not node:
            return

        delay = 2 ** retries
        await asyncio.sleep(delay)
        await self._publish_task(node.task, session_id)

    async def _update_task_status(
        self, session_id: str, task_id: str, status: TaskStatus
    ) -> None:
        """Update task status in DAG and emit status broadcast."""

        state = self._sessions.get(session_id)
        if not state:
            return
        state.dag.set_status(task_id, status)
        await self._persist_dag(session_id)
        await self._broadcast_status(session_id)

    async def _persist_dag(self, session_id: str) -> None:
        """Persist DAG state to Redis."""

        state = self._sessions.get(session_id)
        if not state:
            return
        key = SESSION_DAG_KEY.format(session_id=session_id)
        await self.message_bus._redis.set(key, state.dag.to_json())

    async def _broadcast_status(self, session_id: str) -> None:
        """Broadcast current status summary over the websocket channel."""

        state = self._sessions.get(session_id)
        if not state:
            return
        summary = state.dag.get_status_summary()
        channel = WEBSOCKET_CHANNEL_TEMPLATE.format(session_id=session_id)
        message = AgentMessage(
            type=MessageType.STATUS_UPDATE,
            from_agent=AgentType.PLANNER,
            to_agent=AgentType.PLANNER,
            payload=summary,
            status=TaskStatus.RUNNING,
            confidence=0.0,
        )
        await self.message_bus.publish(channel, message)
