"""Pydantic models for agent messaging and orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .types import AgentType, MessageType, TaskStatus


class AgentMessage(BaseModel):
    """Base message schema exchanged between agents."""

    model_config = ConfigDict(extra="allow")

    id: UUID = Field(default_factory=uuid4)
    type: MessageType
    from_agent: AgentType
    to_agent: AgentType
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: TaskStatus = TaskStatus.PENDING
    confidence: float = Field(default=0.0)

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """Ensure confidence is within the [0, 1] range."""

        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return value


class TaskMessage(AgentMessage):
    """Message schema for task assignment and progress tracking."""

    task_id: UUID
    parent_task_id: Optional[UUID] = None
    depth: int = Field(default=0, ge=0)


class ResearchQuery(BaseModel):
    """Input schema for user research requests."""

    model_config = ConfigDict(extra="forbid")

    user_query: str
    session_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentResult(BaseModel):
    """Represents the structured output of an agent task."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    agent_type: AgentType
    content: str
    sources: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0)

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """Ensure confidence is within the [0, 1] range."""

        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return value
