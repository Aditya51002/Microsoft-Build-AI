"""Task DAG representation for ResearchSwarm sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from core.schemas import AgentResult, TaskMessage
from core.types import TaskStatus


@dataclass
class TaskNode:
    """Represents a single task and its dependency metadata."""

    task: TaskMessage
    depends_on: Set[str] = field(default_factory=set)
    status: TaskStatus = TaskStatus.PENDING
    retries: int = 0
    result: Optional[Dict[str, object]] = None
    error: Optional[str] = None


class TaskDAG:
    """Execution graph for a single research session."""

    def __init__(self, session_id: str) -> None:
        """Initialize an empty DAG for a session."""

        self.session_id = session_id
        self._nodes: Dict[str, TaskNode] = {}

    def add_task(self, task: TaskMessage, depends_on: List[str] | None = None) -> None:
        """Add a task to the DAG with optional dependencies."""

        task_id = str(task.task_id)
        deps = set(depends_on or [])
        self._nodes[task_id] = TaskNode(task=task, depends_on=deps)

    def get_ready_tasks(self) -> List[TaskMessage]:
        """Return tasks whose dependencies are complete."""

        ready: List[TaskMessage] = []
        for task_id, node in self._nodes.items():
            if node.status not in {TaskStatus.PENDING, TaskStatus.RETRY}:
                continue
            if all(
                self._nodes[dep].status == TaskStatus.DONE
                for dep in node.depends_on
                if dep in self._nodes
            ):
                ready.append(node.task)
        return ready

    def mark_done(self, task_id: str, result: AgentResult) -> None:
        """Mark a task as done and store its result."""

        node = self._nodes.get(task_id)
        if not node:
            return
        node.status = TaskStatus.DONE
        node.result = result.model_dump(mode="json")
        node.error = None

    def mark_failed(self, task_id: str, error: str) -> None:
        """Mark a task as failed and store the error."""

        node = self._nodes.get(task_id)
        if not node:
            return
        node.status = TaskStatus.FAILED
        node.error = error

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        """Update task status without modifying results."""

        node = self._nodes.get(task_id)
        if not node:
            return
        node.status = status

    def increment_retry(self, task_id: str) -> int:
        """Increment retry count for a task and return the new value."""

        node = self._nodes.get(task_id)
        if not node:
            return 0
        node.retries += 1
        node.status = TaskStatus.RETRY
        return node.retries

    def is_complete(self) -> bool:
        """Return True if all tasks are done."""

        return all(node.status == TaskStatus.DONE for node in self._nodes.values())

    def get_status_summary(self) -> Dict[str, object]:
        """Summarize current DAG status for status broadcasts."""

        counts = {status.value: 0 for status in TaskStatus}
        tasks: List[Dict[str, object]] = []

        for task_id, node in self._nodes.items():
            counts[node.status.value] = counts.get(node.status.value, 0) + 1
            agent_type = (
                node.task.to_agent.value
                if hasattr(node.task.to_agent, "value")
                else str(node.task.to_agent)
            )
            status_value = (
                node.status.value if hasattr(node.status, "value") else str(node.status)
            )
            tasks.append(
                {
                    "task_id": task_id,
                    "agent_type": agent_type,
                    "status": status_value,
                    "depends_on": list(node.depends_on),
                    "retries": node.retries,
                }
            )

        return {
            "session_id": self.session_id,
            "counts": counts,
            "tasks": tasks,
            "complete": self.is_complete(),
        }

    def to_json(self) -> str:
        """Serialize the DAG state to JSON."""

        payload = {
            "session_id": self.session_id,
            "nodes": {
                task_id: {
                    "task": node.task.model_dump(mode="json"),
                    "depends_on": list(node.depends_on),
                    "status": node.status.value,
                    "retries": node.retries,
                    "result": node.result,
                    "error": node.error,
                }
                for task_id, node in self._nodes.items()
            },
        }
        return json.dumps(payload)

    @staticmethod
    def from_json(data: str) -> "TaskDAG":
        """Rehydrate a TaskDAG from JSON."""

        payload = json.loads(data)
        dag = TaskDAG(payload["session_id"])
        nodes = payload.get("nodes", {})
        for task_id, node in nodes.items():
            task = TaskMessage.model_validate(node["task"])
            dag._nodes[task_id] = TaskNode(
                task=task,
                depends_on=set(node.get("depends_on", [])),
                status=TaskStatus(node.get("status", TaskStatus.PENDING.value)),
                retries=node.get("retries", 0),
                result=node.get("result"),
                error=node.get("error"),
            )
        return dag
