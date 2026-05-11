"""Planner agent implementation for task decomposition."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List
from uuid import UUID, uuid4

from anthropic import AsyncAnthropic

from core.message_bus import MessageBus
from core.schemas import AgentResult, TaskMessage
from core.types import AgentType, MessageType, TaskStatus
from agents.base_agent import BaseAgent


class PlannerAgent(BaseAgent):
    """Agent that decomposes user queries into parallelizable research tasks."""

    def __init__(self, message_bus: MessageBus, anthropic_client: AsyncAnthropic) -> None:
        """Initialize the planner agent with required dependencies."""

        super().__init__(AgentType.PLANNER, message_bus, anthropic_client)

    async def process(self, message: TaskMessage) -> AgentResult:
        """Plan a set of research tasks for the orchestrator to dispatch."""

        self._sync_session_from_message(message)
        user_query = self._extract_user_query(message)
        plan = await self._request_plan(user_query)

        return AgentResult(
            task_id=message.task_id,
            agent_type=AgentType.PLANNER,
            content=json.dumps(plan),
            sources=[],
            confidence=0.9,
        )

    async def _request_plan(self, user_query: str) -> Dict[str, Any]:
        """Call Claude to generate and validate a planning response."""

        if self._demo_mode_enabled():
            return self._demo_plan(user_query)

        try:
            response = await self._call_claude(user_query, strict=False)
            return self._validate_plan(response)
        except (ValueError, json.JSONDecodeError):
            response = await self._call_claude(user_query, strict=True)
            return self._validate_plan(response)

    async def _call_claude(self, user_query: str, strict: bool) -> Dict[str, Any]:
        """Invoke Claude and return parsed JSON output."""

        # The system prompt enforces JSON-only output to simplify parsing and reliability.
        system_prompt = (
            "You are the Planner in a multi-agent research system. "
            "Decompose the user query into 3-6 specific sub-questions that "
            "independent researchers can answer via web search. "
            "Return ONLY valid JSON with no markdown, no extra text."
        )
        if strict:
            # The stricter prompt adds explicit schema reminders to reduce format drift.
            system_prompt += (
                " Respond with a single JSON object matching the required schema. "
                "Do not include commentary, prefixes, or suffixes."
            )

        user_prompt = {
            "user_query": user_query,
            "required_format": {
                "tasks": [
                    {
                        "id": "uuid",
                        "sub_question": "string",
                        "search_keywords": ["string"],
                        "priority": 1,
                    }
                ],
                "synthesis_guidance": "string",
            },
        }

        response = await self._call_anthropic(
            self.anthropic_client.messages.create,
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            temperature=0.3,
            system=system_prompt,
            messages=[{"role": "user", "content": json.dumps(user_prompt)}],
        )

        text = self._extract_text(response)
        return json.loads(text)

    def _extract_text(self, response: Any) -> str:
        """Extract the text content from an Anthropic response."""

        parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "".join(parts).strip()

    def _validate_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize Claude output for downstream use."""

        if not isinstance(plan, dict):
            raise ValueError("Plan must be a JSON object")

        tasks = plan.get("tasks")
        guidance = plan.get("synthesis_guidance")
        if not isinstance(tasks, list) or not isinstance(guidance, str):
            raise ValueError("Plan must include tasks and synthesis_guidance")

        if not 3 <= len(tasks) <= 6:
            raise ValueError("Plan must include 3-6 tasks")

        normalized_tasks: List[Dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("Each task must be an object")

            sub_question = task.get("sub_question")
            search_keywords = task.get("search_keywords")
            priority = task.get("priority")

            if not isinstance(sub_question, str):
                raise ValueError("Task sub_question must be a string")
            if not isinstance(search_keywords, list) or not all(
                isinstance(item, str) for item in search_keywords
            ):
                raise ValueError("Task search_keywords must be a list of strings")
            if not isinstance(priority, int):
                raise ValueError("Task priority must be an integer")

            task_id = task.get("id")
            try:
                task_uuid = UUID(task_id) if task_id else uuid4()
            except (ValueError, TypeError):
                task_uuid = uuid4()

            normalized_tasks.append(
                {
                    "id": str(task_uuid),
                    "sub_question": sub_question.strip(),
                    "search_keywords": [item.strip() for item in search_keywords],
                    "priority": priority,
                }
            )

        return {"tasks": normalized_tasks, "synthesis_guidance": guidance.strip()}

    def _extract_user_query(self, message: TaskMessage) -> str:
        """Extract the user query from the task payload."""

        query = message.payload.get("user_query") or message.payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Task payload must include user_query")
        return query.strip()

    def _demo_plan(self, user_query: str) -> Dict[str, Any]:
        """Generate a deterministic high-quality plan for offline demos."""

        themes = [
            ("Market demand and user pain", ["market size", "customer pain", user_query]),
            ("Competitive landscape", ["competitors", "alternatives", user_query]),
            ("Technical feasibility and architecture", ["technical feasibility", "system design", user_query]),
            ("Risk, regulation, and responsible AI", ["regulation", "risk", "responsible AI", user_query]),
            ("Go-to-market and measurable impact", ["go to market", "business impact", user_query]),
        ]
        tasks = []
        for priority, (theme, keywords) in enumerate(themes, start=1):
            tasks.append(
                {
                    "id": str(uuid4()),
                    "sub_question": f"For '{user_query}', evaluate {theme.lower()}.",
                    "search_keywords": keywords,
                    "priority": priority,
                }
            )
        return {
            "tasks": tasks,
            "synthesis_guidance": (
                "Prioritize decision-grade evidence, cite every important claim, "
                "and call out uncertainties that need human review."
            ),
        }


async def run_planner_test() -> None:
    """Run a standalone planner test and print the decomposed tasks."""

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    bus = MessageBus(redis_url)
    planner = PlannerAgent(bus, client)

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.PLANNER,
        to_agent=AgentType.PLANNER,
        payload={
            "user_query": "What is the market opportunity for solar energy in Southeast Asia?",
            "session_id": "planner-test",
        },
        status=TaskStatus.PENDING,
        confidence=0.9,
        task_id=uuid4(),
        parent_task_id=None,
        depth=0,
    )

    result = await planner.process(task)
    print(json.dumps(json.loads(result.content), indent=2))


if __name__ == "__main__":
    asyncio.run(run_planner_test())
