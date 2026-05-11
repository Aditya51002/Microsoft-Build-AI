"""Writer agent implementation for ResearchSwarm."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID, uuid4

from anthropic import AsyncAnthropic

from agents.base_agent import BaseAgent
from core.message_bus import MessageBus
from core.schemas import AgentMessage, AgentResult, TaskMessage
from core.types import AgentType, MessageType, TaskStatus

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_TEMPERATURE = 0.7
CLAUDE_MAX_TOKENS = 2000
CLAUDE_TIMEOUT_SECONDS = 90

SYSTEM_PROMPT = (
    "You are an expert research writer. You receive synthesised research findings "
    "with critic notes and produce a clear, well-structured report. Use markdown. "
    "Include an executive summary, key findings with evidence, limitations, and "
    "conclusion. Cite sources inline."
)

WEBSOCKET_CHANNEL_TEMPLATE = "ws:broadcast:{session_id}"


class RetryableError(RuntimeError):
    """Signals that a task should be retried by the orchestrator."""


class WriterAgent(BaseAgent):
    """Agent that produces a final report from analysis and critique."""

    def __init__(self, message_bus: MessageBus, anthropic_client: AsyncAnthropic) -> None:
        """Initialize the writer agent with required dependencies."""

        super().__init__(AgentType.WRITER, message_bus, anthropic_client)
        self._logger = logging.getLogger("researchswarm.agent.writer")

    async def process(self, message: TaskMessage) -> AgentResult:
        """Generate a final report and stream chunks to the WebSocket channel."""

        self._sync_session_from_message(message)
        payload = message.payload
        analyst_result = payload.get("analyst_result")
        critic_result = payload.get("critic_result")

        if not isinstance(analyst_result, dict) or not isinstance(critic_result, dict):
            raise ValueError("Writer requires analyst_result and critic_result payloads")

        prompt = {
            "analyst_result": analyst_result,
            "critic_result": critic_result,
            "instructions": [
                "Cite sources inline using markdown links.",
                "Ensure the executive summary is concise and decisive.",
            ],
        }

        start_time = time.perf_counter()
        report_text = await self._stream_report(prompt)
        duration = time.perf_counter() - start_time

        sources = self._collect_sources(analyst_result)
        critique = self._parse_content(critic_result)
        confidence = float(critique.get("final_confidence", critic_result.get("confidence", 0.7)))

        report_payload = {
            "report": report_text,
            "metadata": {"duration_seconds": round(duration, 2)},
        }

        return AgentResult(
            task_id=message.task_id,
            agent_type=AgentType.WRITER,
            content=json.dumps(report_payload, ensure_ascii=True),
            sources=sources,
            confidence=confidence,
        )

    async def emit_result(self, result: AgentResult) -> None:
        """Publish the final report to the planner channel for completion."""

        message = AgentMessage(
            type=MessageType.TASK_RESULT,
            from_agent=AgentType.WRITER,
            to_agent=AgentType.PLANNER,
            payload=result.model_dump(mode="json"),
            status=TaskStatus.DONE,
            confidence=result.confidence,
        )
        channel = self.message_bus.channel_name(AgentType.PLANNER, self._session_id)
        await self._publish(channel, message)

    async def handle_error(self, error: Exception, task_id: str) -> None:
        """Handle retries and errors without crashing the agent loop."""

        if isinstance(error, RetryableError):
            await self.emit_status(task_id, TaskStatus.RETRY)
            return
        await super().handle_error(error, task_id)

    async def _stream_report(self, prompt: Dict[str, Any]) -> str:
        """Stream report output from Claude and emit chunks to WebSocket."""

        if self._demo_mode_enabled():
            report = self._demo_report(prompt)
            for index in range(0, len(report), 48):
                await self._emit_stream_chunk(report[index : index + 48])
                await asyncio.sleep(0.03)
            return report

        async def _consume_stream() -> tuple[list[str], Any]:
            stream_context = self.anthropic_client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                temperature=CLAUDE_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(prompt)}],
            )
            if inspect.isawaitable(stream_context):
                stream_context = await stream_context

            async with stream_context as stream:
                report_chunks: List[str] = []
                async for text in stream.text_stream:
                    if not text:
                        continue
                    report_chunks.append(text)
                    await self._emit_stream_chunk(text)
                final_message = await stream.get_final_message()
            return report_chunks, final_message

        try:
            report_chunks, final_message = await self._call_anthropic(_consume_stream)
        except asyncio.TimeoutError as exc:
            raise RetryableError("Claude streaming timed out") from exc

        if not report_chunks:
            report_chunks.append(self._extract_text(final_message))
        return "".join(report_chunks).strip()

    async def _emit_stream_chunk(self, chunk: str) -> None:
        """Emit streamed output chunks to the WebSocket broadcast channel."""

        channel = WEBSOCKET_CHANNEL_TEMPLATE.format(session_id=self._session_id)
        message = AgentMessage(
            type=MessageType.STATUS_UPDATE,
            from_agent=AgentType.WRITER,
            to_agent=AgentType.WRITER,
            payload={"chunk": chunk},
            status=TaskStatus.RUNNING,
            confidence=0.0,
        )
        await self._publish(channel, message)

    def _collect_sources(self, analyst_result: Dict[str, Any]) -> List[str]:
        """Collect unique sources from analyst payload."""

        sources = analyst_result.get("sources")
        if isinstance(sources, list):
            return list(dict.fromkeys(source for source in sources if source))
        return []

    def _parse_content(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Parse an AgentResult content field into a dictionary."""

        content = result.get("content")
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            return {}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_text(self, response: Any) -> str:
        """Extract text blocks from an Anthropic response."""

        parts: List[str] = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "".join(parts).strip()

    def _demo_report(self, prompt: Dict[str, Any]) -> str:
        """Create a deterministic final report for offline demos."""

        analyst_result = prompt.get("analyst_result", {})
        critic_result = prompt.get("critic_result", {})
        analysis = self._parse_content(analyst_result) if isinstance(analyst_result, dict) else {}
        critique = self._parse_content(critic_result) if isinstance(critic_result, dict) else {}
        insights = analysis.get("key_insights", [])
        notes = critique.get("critique_notes", [])

        insight_lines = "\n".join(
            f"- {item}" for item in insights if isinstance(item, str)
        )
        note_lines = "\n".join(f"- {item}" for item in notes if isinstance(item, str))

        return (
            "# ResearchSwarm Decision Brief\n\n"
            "## Executive Summary\n"
            "ResearchSwarm is most compelling as a verifiable research copilot for "
            "business and technical decisions. Its advantage is not simply that it "
            "uses multiple agents, but that each agent has a clear responsibility and "
            "the final answer exposes sources, confidence, and critic review.\n\n"
            "## Key Findings\n"
            f"{insight_lines or '- The system completed planning, research, analysis, critique, and writing.'}\n\n"
            "## Critic Review\n"
            f"{note_lines or '- No blocking critique was raised.'}\n\n"
            "## Limitations\n"
            "- Live web research quality depends on configured model and search access.\n"
            "- A winning submission should include a focused buyer persona and a recorded successful run.\n\n"
            "## Recommendation\n"
            "Demo ResearchSwarm as a trust-first research operating system: show the "
            "agent trace, claim ledger, critic notes, and final cited report in one "
            "tight story."
        )


async def run_writer_test() -> None:
    """Run a standalone writer test with synthetic inputs."""

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    bus = MessageBus(redis_url)
    writer = WriterAgent(bus, client)

    analyst_result = AgentResult(
        task_id=uuid4(),
        agent_type=AgentType.ANALYST,
        content=json.dumps(
            {
                "key_insights": ["Solar expansion is policy-driven."],
                "confidence_map": {"Solar expansion is policy-driven.": 0.7},
                "contradictions": [],
                "gaps": ["Grid integration constraints"],
                "overall_confidence": 0.7,
            }
        ),
        sources=["https://example.com/report"],
        confidence=0.7,
    )

    critic_result = {
        "approved": True,
        "critique_notes": ["Add more regional policy detail."],
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
            "session_id": "writer-test",
        },
        status=TaskStatus.PENDING,
        confidence=0.7,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    result = await writer.process(task)
    print(result.content)


if __name__ == "__main__":
    asyncio.run(run_writer_test())
