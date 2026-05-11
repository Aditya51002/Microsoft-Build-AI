"""Core enums for agent messaging and task orchestration."""

from enum import Enum


class AgentType(str, Enum):
    """Enumerates the agent roles in the system."""

    PLANNER = "PLANNER"
    RESEARCHER = "RESEARCHER"
    ANALYST = "ANALYST"
    CRITIC = "CRITIC"
    WRITER = "WRITER"


class TaskStatus(str, Enum):
    """Represents execution state for tasks and agents."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    RETRY = "RETRY"
    CANCELLED = "CANCELLED"


class MessageType(str, Enum):
    """Defines high-level message categories for the bus."""

    TASK_ASSIGN = "TASK_ASSIGN"
    TASK_RESULT = "TASK_RESULT"
    STATUS_UPDATE = "STATUS_UPDATE"
    ERROR = "ERROR"
    HEARTBEAT = "HEARTBEAT"
