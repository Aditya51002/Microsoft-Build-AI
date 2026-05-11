"""Analyst agent implementation for ResearchSwarm."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID, uuid4

from anthropic import AsyncAnthropic

from agents.base_agent import BaseAgent
from core.message_bus import MessageBus
from core.schemas import AgentMessage, AgentResult, TaskMessage
from core.retry import REDIS_RETRY, retry_with_backoff
from core.types import AgentType, MessageType, TaskStatus

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_TEMPERATURE = 0.2
CLAUDE_MAX_TOKENS = 1200
CLAUDE_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = (
    "You are a senior research analyst. You receive multiple research findings on "
    "different aspects of a question and must synthesise them into coherent insights. "
    "Identify patterns, contradictions, and knowledge gaps. Weight findings by their "
    "confidence scores. Return JSON only."
)

RESULTS_KEY_TEMPLATE = "session:{session_id}:researcher_results"


class RetryableError(RuntimeError):
    """Signals that a task should be retried by the orchestrator."""


class AnalystAgent(BaseAgent):
    """Agent that synthesizes researcher findings into analytic insights."""

    def __init__(self, message_bus: MessageBus, anthropic_client: AsyncAnthropic) -> None:
        """Initialize the analyst agent with required dependencies."""

        super().__init__(AgentType.ANALYST, message_bus, anthropic_client)
        self._logger = logging.getLogger("researchswarm.agent.analyst")

    async def run(self) -> None:
        """Subscribe to all analyst task channels and process assignments."""

        channel_pattern = f"agent:{self.agent_type.value}:*"
        async for message in self.message_bus.subscribe_pattern(channel_pattern):
            if not isinstance(message, TaskMessage):
                self._logger.warning("Ignoring message: %s", message)
                continue

            try:
                self._sync_session_from_message(message)
                result = await self.process(message)
                await self.emit_result(result)
            except Exception as exc:  # pragma: no cover - runtime safety
                task_id = str(getattr(message, "task_id", ""))
                await self.handle_error(exc, task_id)

    async def process(self, message: TaskMessage) -> AgentResult:
        """Wait for researcher results, synthesize insights, and return analysis."""

        self._sync_session_from_message(message)
        session_id = self._session_id

        start_time = time.perf_counter()
        provided_results = message.payload.get("researcher_results")
        if isinstance(provided_results, list):
            results = [item for item in provided_results if isinstance(item, dict)]
        else:
            expected = await self._expected_results(message)
            results = await self._wait_for_researcher_results(session_id, expected)
        duration = time.perf_counter() - start_time

        payload = self._build_payload(results)
        analysis = await self._call_claude(payload)

        analysis["metadata"] = {"duration_seconds": round(duration, 2)}
        overall_confidence = float(analysis.get("overall_confidence", 0.0))

        sources = self._collect_sources(results)

        return AgentResult(
            task_id=message.task_id,
            agent_type=AgentType.ANALYST,
            content=json.dumps(analysis, ensure_ascii=True),
            sources=sources,
            confidence=overall_confidence,
        )

    async def handle_error(self, error: Exception, task_id: str) -> None:
        """Handle retries and errors without crashing the agent loop."""

        if isinstance(error, RetryableError):
            self._logger.warning("Retry requested for task %s: %s", task_id, error)
            await self.emit_status(task_id, TaskStatus.RETRY)
            return
        await super().handle_error(error, task_id)

    async def _expected_results(self, message: TaskMessage) -> int:
        """Determine how many researcher results are expected for this session."""

        payload_count = message.payload.get("expected_results")
        if isinstance(payload_count, int) and payload_count > 0:
            return payload_count

        session_state = await self.message_bus.get_session_state(self._session_id)
        for key in ("expected_researcher_results", "researcher_task_count"):
            value = session_state.get(key)
            if isinstance(value, str) and value.isdigit():
                return int(value)

        return 1

    async def _wait_for_researcher_results(
        self, session_id: str, expected: int
    ) -> List[Dict[str, Any]]:
        """Poll Redis until all researcher results arrive or timeout."""

        deadline = time.time() + 120
        key = RESULTS_KEY_TEMPLATE.format(session_id=session_id)
        redis_client = self.message_bus._redis

        while time.time() < deadline:
            count = await retry_with_backoff(
                redis_client.llen,
                key,
                config=REDIS_RETRY,
            )
            if count >= expected:
                break
            await asyncio.sleep(1)

        count = await retry_with_backoff(
            redis_client.llen,
            key,
            config=REDIS_RETRY,
        )
        if count < expected:
            raise RetryableError("Timed out waiting for researcher results")

        raw_results = await retry_with_backoff(
            redis_client.lrange,
            key,
            0,
            -1,
            config=REDIS_RETRY,
        )
        results: List[Dict[str, Any]] = []
        for item in raw_results:
            try:
                results.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return results

    async def _store_researcher_result(self, message: AgentMessage) -> None:
        """Store researcher results in Redis for aggregation."""

        payload = message.payload if isinstance(message.payload, dict) else {}
        session_id = payload.get("session_id") or self._session_id
        key = RESULTS_KEY_TEMPLATE.format(session_id=session_id)
        await retry_with_backoff(
            self.message_bus._redis.rpush,
            key,
            json.dumps(message.payload),
            config=REDIS_RETRY,
        )

    async def _call_claude(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Call Claude with aggregated findings and parse JSON output."""

        if self._demo_mode_enabled():
            return self._demo_analysis(payload)

        prompt = {
            "analysis_input": payload,
            "required_output": {
                "key_insights": ["string"],
                "confidence_map": {"insight": 0.0},
                "contradictions": ["string"],
                "gaps": ["string"],
                "overall_confidence": 0.0,
            },
            "instructions": [
                "Return JSON only.",
                "Weight findings by confidence scores.",
            ],
        }

        response = await asyncio.wait_for(
            self._call_anthropic(
                self.anthropic_client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                temperature=CLAUDE_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(prompt)}],
            ),
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )

        text = self._extract_text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Claude returned invalid JSON") from exc

    def _build_payload(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Normalize researcher outputs into a single analysis payload."""

        normalized: List[Dict[str, Any]] = []
        for result in results:
            content = result.get("content")
            try:
                parsed = json.loads(content) if isinstance(content, str) else {}
            except json.JSONDecodeError:
                parsed = {}

            normalized.append(
                {
                    "task_id": result.get("task_id"),
                    "confidence": result.get("confidence"),
                    "sources": result.get("sources", []),
                    "findings": parsed.get("findings", []),
                    "summary": parsed.get("summary", ""),
                    "key_data_points": parsed.get("key_data_points", []),
                }
            )

        return {"researcher_results": normalized}

    def _collect_sources(self, results: Iterable[Dict[str, Any]]) -> List[str]:
        """Collect unique sources across researcher results."""

        sources: List[str] = []
        for result in results:
            for source in result.get("sources", []) or []:
                if source not in sources:
                    sources.append(source)
        return sources

    def _extract_text(self, response: Any) -> str:
        """Extract text blocks from an Anthropic response."""

        parts: List[str] = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "".join(parts).strip()

    def _demo_analysis(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create deterministic synthesis for offline demos."""

        results = payload.get("researcher_results", [])
        summaries = []
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, dict):
                    continue
                content = result.get("content")
                try:
                    parsed = json.loads(content) if isinstance(content, str) else {}
                except json.JSONDecodeError:
                    parsed = {}
                summary = parsed.get("summary")
                if isinstance(summary, str) and summary:
                    summaries.append(summary)

        return {
            "key_insights": [
                "The opportunity is strongest when the product is framed around evidence-backed decisions rather than generic chat.",
                "The multi-agent design is valuable because it separates planning, evidence gathering, synthesis, critique, and writing.",
                "The largest execution risk is trust: every claim should expose its source, confidence, and critic status.",
            ],
            "confidence_map": {
                "Evidence-backed decision workflow": 0.82,
                "Separated agent responsibilities": 0.86,
                "Trust layer as differentiator": 0.8,
            },
            "contradictions": [],
            "gaps": [
                "Add explicit customer persona and measurable business outcome.",
                "Add production observability and Microsoft Azure deployment evidence.",
            ],
            "supporting_summaries": summaries[:5],
            "overall_confidence": 0.82,
        }


async def run_analyst_test() -> None:
    """Run a standalone analyst test with synthetic researcher data."""

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    bus = MessageBus(redis_url)
    analyst = AnalystAgent(bus, client)

    session_id = "analyst-test"
    analyst._session_id = session_id

    fake_result = AgentResult(
        task_id=uuid4(),
        agent_type=AgentType.RESEARCHER,
        content=json.dumps(
            {
                "findings": [
                    {
                        "fact": "Vietnam installed solar capacity exceeded 18 GW by 2023.",
                        "source": "https://example.com/report",
                        "confidence": 0.8,
                    }
                ],
                "summary": "Vietnam expanded utility-scale solar after 2019.",
                "key_data_points": ["18 GW installed capacity"],
            }
        ),
        sources=["https://example.com/report"],
        confidence=0.8,
    )

    await analyst._store_researcher_result(
        AgentMessage(
            type=MessageType.TASK_RESULT,
            from_agent=AgentType.RESEARCHER,
            to_agent=AgentType.ANALYST,
            payload=fake_result.model_dump(mode="json"),
            status=TaskStatus.DONE,
            confidence=0.8,
        )
    )

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.PLANNER,
        to_agent=AgentType.ANALYST,
        payload={"expected_results": 1, "session_id": session_id},
        status=TaskStatus.PENDING,
        confidence=0.9,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    result = await analyst.process(task)
    print(result.content)


if __name__ == "__main__":
    asyncio.run(run_analyst_test())
