"""Shared base class for ResearchSwarm agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Optional

from anthropic import AsyncAnthropic

from core.message_bus import MessageBus
from core.schemas import AgentMessage, TaskMessage, AgentResult
from core.retry import ANTHROPIC_RETRY, REDIS_RETRY, retry_with_backoff
from core.types import AgentType, MessageType, TaskStatus


class BaseAgent(ABC):
    """Abstract base class that handles message bus wiring and error safety."""

    def __init__(
        self,
        agent_type: AgentType,
        message_bus: MessageBus,
        anthropic_client: AsyncAnthropic,
    ) -> None:
        """Initialize the agent with its type, bus, and LLM client."""

        self.agent_type = agent_type
        self.message_bus = message_bus
        self.anthropic_client = anthropic_client
        self._logger = logging.getLogger(f"researchswarm.agent.{agent_type.value.lower()}")
        self._session_id = os.environ.get("SESSION_ID", "default")
        self._heartbeat_task: Optional[asyncio.Task] = None

    @abstractmethod
    async def process(self, message: TaskMessage) -> AgentResult:
        """Process a task message and return an agent result."""

    async def run(self) -> None:
        """Subscribe to all session channels for this agent and process tasks."""

        channel_pattern = f"agent:{self.agent_type.value}:*"
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async for message in self.message_bus.subscribe_pattern(channel_pattern):
                if not isinstance(message, TaskMessage):
                    self._logger.warning("Ignoring non-task message: %s", message)
                    continue
                task_id = str(getattr(message, "task_id", ""))
                start = time.perf_counter()
                try:
                    self._sync_session_from_message(message)
                    self._log_action("process", task_id, "running", 0.0)
                    result = await self.process(message)
                    await self.emit_result(result)
                    duration_ms = (time.perf_counter() - start) * 1000
                    self._log_action("process", task_id, "done", duration_ms)
                except Exception as exc:  # pragma: no cover - runtime safety
                    duration_ms = (time.perf_counter() - start) * 1000
                    self._log_action("process", task_id, "error", duration_ms)
                    await self.handle_error(exc, task_id)
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()

    async def emit_result(self, result: AgentResult) -> None:
        """Publish a task result to the orchestrator channel."""

        payload = result.model_dump(mode="json")
        message = AgentMessage(
            type=MessageType.TASK_RESULT,
            from_agent=self.agent_type,
            to_agent=AgentType.PLANNER,
            payload=payload,
            status=TaskStatus.DONE,
            confidence=result.confidence,
        )
        channel = self.message_bus.channel_name(AgentType.PLANNER, self._session_id)
        await self._publish(channel, message)
        self._log_action("emit_result", str(result.task_id), "done", 0.0)

    async def emit_status(self, task_id: str, status: TaskStatus) -> None:
        """Publish a status update for a task to the orchestrator."""

        message = AgentMessage(
            type=MessageType.STATUS_UPDATE,
            from_agent=self.agent_type,
            to_agent=AgentType.PLANNER,
            payload={"task_id": task_id},
            status=status,
            confidence=0.0,
        )
        channel = self.message_bus.channel_name(AgentType.PLANNER, self._session_id)
        await self._publish(channel, message)
        self._log_action("emit_status", task_id, status.value, 0.0)

    async def handle_error(self, error: Exception, task_id: str) -> None:
        """Handle errors by emitting status updates and logging details."""

        self._logger.exception("Task %s failed: %s", task_id, error)
        message = AgentMessage(
            type=MessageType.ERROR,
            from_agent=self.agent_type,
            to_agent=AgentType.PLANNER,
            payload={"task_id": task_id, "error": str(error)},
            status=TaskStatus.FAILED,
            confidence=0.0,
        )
        channel = self.message_bus.channel_name(AgentType.PLANNER, self._session_id)
        await self._publish(channel, message)
        self._log_action("handle_error", task_id, "error", 0.0)

    def _sync_session_from_message(self, message: TaskMessage) -> None:
        """Update the session id from message payload when provided."""

        session_id = message.payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id

    def _demo_mode_enabled(self) -> bool:
        """Return True when deterministic local demo output should be used."""

        explicit = os.environ.get("RESEARCHSWARM_DEMO_MODE", "").lower()
        if explicit in {"1", "true", "yes", "on"}:
            return True
        return not bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    async def _publish(self, channel: str, message: AgentMessage) -> None:
        """Publish a message with Redis retry handling."""

        await retry_with_backoff(
            self.message_bus.publish,
            channel,
            message,
            config=REDIS_RETRY,
        )

    async def _call_anthropic(self, func, *args, **kwargs):
        """Invoke Anthropic API with retry handling."""

        return await retry_with_backoff(
            func,
            *args,
            config=ANTHROPIC_RETRY,
            **kwargs,
        )

    async def _heartbeat_loop(self) -> None:
        """Emit periodic heartbeat messages to Redis."""

        while True:
            channel = self.message_bus.channel_name(AgentType.PLANNER, self._session_id)
            heartbeat = AgentMessage(
                type=MessageType.HEARTBEAT,
                from_agent=self.agent_type,
                to_agent=AgentType.PLANNER,
                payload={"session_id": self._session_id, "agent": self.agent_type.value},
                status=TaskStatus.RUNNING,
                confidence=0.0,
            )
            await self._publish(channel, heartbeat)
            await asyncio.sleep(10)

    def _log_action(self, action: str, task_id: str, status: str, duration_ms: float) -> None:
        """Log a structured agent action as JSON."""

        payload = {
            "agent": self.agent_type.value,
            "action": action,
            "task_id": task_id,
            "duration_ms": round(duration_ms, 2),
            "status": status,
        }
        self._logger.info(json.dumps(payload))
