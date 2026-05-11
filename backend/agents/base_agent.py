"""Shared base class for ResearchSwarm agents."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

from anthropic import AsyncAnthropic

from core.message_bus import MessageBus
from core.schemas import AgentMessage, TaskMessage, AgentResult
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

    @abstractmethod
    async def process(self, message: TaskMessage) -> AgentResult:
        """Process a task message and return an agent result."""

    async def run(self) -> None:
        """Subscribe to the agent channel and process incoming tasks."""

        channel = self.message_bus.channel_name(self.agent_type, self._session_id)
        async for message in self.message_bus.subscribe(channel):
            if not isinstance(message, TaskMessage):
                self._logger.warning("Ignoring non-task message: %s", message)
                continue
            try:
                self._sync_session_from_message(message)
                result = await self.process(message)
                await self.emit_result(result)
            except Exception as exc:  # pragma: no cover - runtime safety
                task_id = str(getattr(message, "task_id", ""))
                await self.handle_error(exc, task_id)

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
        await self.message_bus.publish(channel, message)

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
        await self.message_bus.publish(channel, message)

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
        await self.message_bus.publish(channel, message)

    def _sync_session_from_message(self, message: TaskMessage) -> None:
        """Update the session id from message payload when provided."""

        session_id = message.payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
