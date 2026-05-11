"""Researcher agent implementation using Claude web search tool use."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID, uuid4

from anthropic import AsyncAnthropic

from agents.base_agent import BaseAgent
from core.message_bus import MessageBus
from core.schemas import AgentMessage, AgentResult, TaskMessage
from core.retry import SEARCH_RETRY, retry_with_backoff
from core.types import AgentType, MessageType, TaskStatus

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_TEMPERATURE = 0.2
CLAUDE_MAX_TOKENS = 2000
CLAUDE_TIMEOUT_SECONDS = 90

SYSTEM_PROMPT = (
    "You are a precise research agent. You receive a specific research sub-question "
    "and must find accurate, current information using web search. "
    "Search at least 3 times with different queries to triangulate findings. "
    "For each piece of information, note the source URL. "
    "Return your findings as structured JSON only."
)

ADJECTIVE_STOPWORDS = {
    "current",
    "recent",
    "latest",
    "new",
    "major",
    "leading",
    "emerging",
    "global",
    "regional",
    "local",
    "rapid",
    "accelerating",
    "significant",
    "large",
    "small",
    "high",
    "low",
    "annual",
    "quarterly",
    "monthly",
    "yearly",
    "estimated",
    "approximate",
}

URL_PATTERN = re.compile(r"https?://\S+")


class RetryableError(RuntimeError):
    """Signals that a task should be retried by the orchestrator."""


class ResearcherAgent(BaseAgent):
    """Agent that performs web research and returns structured findings."""

    def __init__(self, message_bus: MessageBus, anthropic_client: AsyncAnthropic) -> None:
        """Initialize the researcher agent with required dependencies."""

        super().__init__(AgentType.RESEARCHER, message_bus, anthropic_client)
        self._logger = logging.getLogger("researchswarm.agent.researcher")

    async def process(self, message: TaskMessage) -> AgentResult:
        """Run web research for a sub-question and return structured findings."""

        self._sync_session_from_message(message)
        task_id = str(message.task_id)
        sub_question = self._require_str(message.payload, "sub_question")
        search_keywords = self._require_str_list(message.payload, "search_keywords")

        start_time = time.perf_counter()
        if self._demo_mode_enabled():
            parsed, raw_text, sources, result_count = self._demo_research(
                sub_question, search_keywords
            )
        else:
            try:
                parsed, raw_text, sources, result_count = await self._research(
                    sub_question, search_keywords
                )
                if result_count == 0:
                    broader = self._broaden_keywords(search_keywords)
                    self._logger.warning(
                        "No web search results for task %s; retrying with broader keywords: %s",
                        task_id,
                        broader,
                    )
                    parsed, raw_text, sources, _ = await self._research(
                        sub_question, broader
                    )
            except asyncio.TimeoutError as exc:
                raise RetryableError("Claude request timed out") from exc

        duration = time.perf_counter() - start_time

        if parsed is None:
            # JSON parsing failed; provide a best-effort summary with reduced confidence.
            parsed = {
                "findings": [],
                "summary": raw_text.strip(),
                "key_data_points": [],
            }
            confidence = 0.5
            sources = sources or self._extract_urls(raw_text)
        else:
            confidence = self._average_confidence(parsed.get("findings", []))

        parsed["metadata"] = {"duration_seconds": round(duration, 2)}

        return AgentResult(
            task_id=message.task_id,
            agent_type=AgentType.RESEARCHER,
            content=json.dumps(parsed, ensure_ascii=True),
            sources=sources,
            confidence=confidence,
        )

    async def handle_error(self, error: Exception, task_id: str) -> None:
        """Handle retries and errors without crashing the agent loop."""

        if isinstance(error, RetryableError):
            self._logger.warning("Retry requested for task %s: %s", task_id, error)
            await self.emit_status(task_id, TaskStatus.RETRY)
            return
        await super().handle_error(error, task_id)

    async def _research(
        self, sub_question: str, search_keywords: List[str]
    ) -> Tuple[Optional[Dict[str, Any]], str, List[str], int]:
        """Execute a research pass and return parsed JSON, raw text, sources, and result count."""

        messages = [
            {
                "role": "user",
                # The user prompt embeds the exact schema to keep the model output machine-parseable.
                "content": json.dumps(
                    {
                        "sub_question": sub_question,
                        "search_keywords": search_keywords,
                        "required_output": {
                            "findings": [
                                {
                                    "fact": "string",
                                    "source": "url",
                                    "confidence": 0.0,
                                }
                            ],
                            "summary": "string",
                            "key_data_points": ["string"],
                        },
                        "instructions": [
                            "Run at least three distinct web searches.",
                            "Return only JSON with the required keys.",
                        ],
                    },
                    ensure_ascii=True,
                ),
            }
        ]

        text, result_count = await self._collect_claude_output(messages)
        sources = self._extract_urls(text)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None

        return parsed, text, sources, result_count

    async def _collect_claude_output(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[str, int]:
        """Loop on tool use until Claude returns a final response."""

        result_count = 0
        while True:
            response = await asyncio.wait_for(
                self._call_anthropic(
                    self.anthropic_client.messages.create,
                    model=CLAUDE_MODEL,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    temperature=CLAUDE_TEMPERATURE,
                    system=SYSTEM_PROMPT,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                    messages=messages,
                ),
                timeout=CLAUDE_TIMEOUT_SECONDS,
            )

            result_count += self._log_tool_activity(response)
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                return self._extract_text(response), result_count

            if response.stop_reason == "tool_use":
                tool_results = await self._resolve_tool_use(response)
                messages.append({"role": "user", "content": tool_results})
                continue

            raise RuntimeError(f"Unexpected stop_reason: {response.stop_reason}")

    async def _resolve_tool_use(self, response: Any) -> List[Dict[str, Any]]:
        """Run built-in web search for each tool call and return tool_result blocks."""

        tool_results: List[Dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            if getattr(block, "name", "") != "web_search":
                continue

            query = block.input.get("query") if isinstance(block.input, dict) else None
            if not query:
                continue

            results = await self._execute_web_search(query)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": results,
                }
            )

        if not tool_results:
            raise RuntimeError("Tool use requested but no web_search calls were found")

        return tool_results

    async def _execute_web_search(self, query: str) -> List[Dict[str, Any]]:
        """Execute the Anthropic web search tool and normalize results."""

        self._logger.info("web_search query: %s", query)

        client_tools = getattr(self.anthropic_client, "tools", None)
        beta_tools = getattr(getattr(self.anthropic_client, "beta", None), "tools", None)

        if client_tools and hasattr(client_tools, "web_search"):
            raw = await retry_with_backoff(
                client_tools.web_search,
                query=query,
                config=SEARCH_RETRY,
            )
        elif beta_tools and hasattr(beta_tools, "web_search"):
            raw = await retry_with_backoff(
                beta_tools.web_search,
                query=query,
                config=SEARCH_RETRY,
            )
        else:
            raise RuntimeError("Anthropic web_search tool is not available in this SDK")

        results = self._normalize_search_results(raw)
        self._logger.info("web_search results: %s", len(results))
        return results

    def _normalize_search_results(self, raw: Any) -> List[Dict[str, Any]]:
        """Normalize web search results into tool_result content blocks."""

        if isinstance(raw, dict):
            items = raw.get("results") or raw.get("data") or []
        elif hasattr(raw, "results"):
            items = raw.results
        else:
            items = []

        normalized: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("link")
            if not url:
                continue
            normalized.append(
                {
                    "type": "web_search_result",
                    "url": url,
                    "title": item.get("title") or item.get("name") or "",
                    "content": item.get("content") or item.get("snippet") or "",
                }
            )
        return normalized

    def _log_tool_activity(self, response: Any) -> int:
        """Log search queries and result counts from tool-related blocks."""

        result_count = 0
        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "tool_use" and getattr(block, "name", "") == "web_search":
                query = block.input.get("query") if isinstance(block.input, dict) else None
                if query:
                    self._logger.info("web_search query: %s", query)
            if block_type == "tool_result":
                content = getattr(block, "content", [])
                if isinstance(content, list):
                    count = sum(
                        1 for item in content if isinstance(item, dict) and item.get("type")
                    )
                    result_count += count
                    self._logger.info("web_search results: %s", count)
        return result_count

    def _average_confidence(self, findings: Iterable[Dict[str, Any]]) -> float:
        """Compute average confidence across findings."""

        scores = [item.get("confidence") for item in findings if isinstance(item, dict)]
        normalized = [score for score in scores if isinstance(score, (int, float))]
        if not normalized:
            return 0.0
        return float(sum(normalized) / len(normalized))

    def _broaden_keywords(self, keywords: List[str]) -> List[str]:
        """Remove common adjectives to broaden search keywords."""

        broadened: List[str] = []
        for phrase in keywords:
            tokens = [token for token in phrase.split() if token]
            kept = [
                token
                for token in tokens
                if token.lower() not in ADJECTIVE_STOPWORDS
            ]
            broadened.append(" ".join(kept) if kept else phrase)
        return list(dict.fromkeys(broadened))

    def _extract_text(self, response: Any) -> str:
        """Extract text blocks from an Anthropic response."""

        parts = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "".join(parts).strip()

    def _extract_urls(self, text: str) -> List[str]:
        """Extract URLs from text content."""

        return list(dict.fromkeys(URL_PATTERN.findall(text)))

    def _demo_research(
        self, sub_question: str, search_keywords: List[str]
    ) -> Tuple[Dict[str, Any], str, List[str], int]:
        """Return deterministic research findings for offline demos."""

        source = "https://learn.microsoft.com/azure/architecture/ai-ml/"
        focus = self._demo_focus(sub_question)
        keyword_text = ", ".join(search_keywords[:3])
        parsed = {
            "findings": [
                {
                    "fact": f"The {focus} dimension should be evaluated with explicit evidence, uncertainty, and human review checkpoints.",
                    "source": source,
                    "confidence": 0.78,
                },
                {
                    "fact": f"Relevant evaluation signals include {keyword_text}; together they map to product fit, feasibility, and risk.",
                    "source": "https://learn.microsoft.com/azure/architecture/guide/",
                    "confidence": 0.72,
                },
            ],
            "summary": (
                "The strongest answer will combine market evidence, architecture quality, "
                "responsible AI controls, and a clear operational workflow."
            ),
            "key_data_points": [
                "Evidence traceability is required for trustworthy research output.",
                "Human-in-the-loop review improves usability for high-stakes decisions.",
            ],
        }
        return parsed, json.dumps(parsed), [source, "https://learn.microsoft.com/azure/architecture/guide/"], 2

    def _demo_focus(self, sub_question: str) -> str:
        """Extract a readable focus phrase from a demo sub-question."""

        lowered = sub_question.lower()
        marker = "evaluate "
        if marker in lowered:
            start = lowered.index(marker) + len(marker)
            focus = sub_question[start:].strip().rstrip(".")
            return focus or "research"
        return "research"

    def _require_str(self, payload: Dict[str, Any], key: str) -> str:
        """Fetch a required string field from payload."""

        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Task payload must include {key}")
        return value.strip()

    def _require_str_list(self, payload: Dict[str, Any], key: str) -> List[str]:
        """Fetch a required list of strings from payload."""

        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Task payload must include {key} as list[str]")
        return [item.strip() for item in value if item.strip()]


async def run_researcher_test() -> None:
    """Run a standalone researcher test and print the findings JSON."""

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    bus = MessageBus(redis_url)
    researcher = ResearcherAgent(bus, client)

    task = TaskMessage(
        type=MessageType.TASK_ASSIGN,
        from_agent=AgentType.PLANNER,
        to_agent=AgentType.RESEARCHER,
        payload={
            "sub_question": "What is the current solar energy capacity in Vietnam?",
            "search_keywords": [
                "Vietnam solar energy capacity",
                "Vietnam solar power installed capacity",
                "Vietnam renewable energy statistics",
            ],
            "session_id": "researcher-test",
        },
        status=TaskStatus.PENDING,
        confidence=0.9,
        task_id=uuid4(),
        parent_task_id=None,
        depth=1,
    )

    result = await researcher.process(task)
    print(result.content)


if __name__ == "__main__":
    asyncio.run(run_researcher_test())
