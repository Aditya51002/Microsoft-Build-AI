"""Redis-backed message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator, Dict

import redis.asyncio as redis

from .schemas import AgentMessage, TaskMessage
from .types import AgentType, MessageType, TaskStatus


class MessageBus:
    """Wraps Redis pub/sub for agent messaging and state tracking."""

    def __init__(self, redis_url: str) -> None:
        """Initialize the message bus with a Redis connection URL."""

        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def publish(self, channel: str, message: AgentMessage) -> None:
        """Publish a message to a Redis channel as JSON."""

        payload = message.model_dump(mode="json")
        await self._redis.publish(channel, json.dumps(payload))

        if channel.startswith("ws:broadcast:"):
            session_id = channel.split("ws:broadcast:", 1)[-1]
            key = f"session:{session_id}:events"
            event_payload = {
                "timestamp_ms": int(time.time() * 1000),
                "event": payload,
            }
            try:
                await self._redis.rpush(key, json.dumps(event_payload))
            except Exception:
                pass

    async def subscribe(self, channel: str) -> AsyncIterator[AgentMessage]:
        """Subscribe to a Redis channel and yield AgentMessage instances."""

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                data = json.loads(raw.get("data", "{}"))
                yield self._deserialize_message(data)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def subscribe_pattern(self, pattern: str) -> AsyncIterator[AgentMessage]:
        """Subscribe to Redis channels matching a pattern and yield messages."""

        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(pattern)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "pmessage":
                    continue
                data = json.loads(raw.get("data", "{}"))
                yield self._deserialize_message(data)
        finally:
            await pubsub.punsubscribe(pattern)
            await pubsub.close()

    async def get_session_state(self, session_id: str) -> Dict[str, Any]:
        """Retrieve session state data from Redis."""

        key = f"session:{session_id}:state"
        return await self._redis.hgetall(key)

    async def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        """Persist task status in Redis as a hash entry."""

        key = f"task:{task_id}:state"
        await self._redis.hset(key, mapping={"status": status.value})

    @staticmethod
    def channel_name(agent_type: AgentType | str, session_id: str) -> str:
        """Build the canonical channel name for an agent and session."""

        value = agent_type.value if hasattr(agent_type, "value") else str(agent_type)
        return f"agent:{value}:{session_id}"

    @staticmethod
    def _deserialize_message(data: Dict[str, Any]) -> AgentMessage:
        """Deserialize raw JSON into the appropriate message model."""

        if "task_id" in data:
            return TaskMessage.model_validate(data)
        return AgentMessage.model_validate(data)


async def _run_test() -> None:
    """Publish and read back a test message for local validation."""

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    bus = MessageBus(redis_url)
    session_id = "test-session"
    channel = bus.channel_name(AgentType.PLANNER, session_id)

    test_message = AgentMessage(
        type=MessageType.HEARTBEAT,
        from_agent=AgentType.PLANNER,
        to_agent=AgentType.RESEARCHER,
        payload={"note": "ping"},
        status=TaskStatus.RUNNING,
        confidence=0.9,
    )

    async def read_once() -> None:
        """Read a single message from the channel and print it."""

        async for message in bus.subscribe(channel):
            print("Received:", message.model_dump(mode="json"))
            break

    reader_task = asyncio.create_task(read_once())
    await asyncio.sleep(0.1)
    await bus.publish(channel, test_message)
    await asyncio.wait_for(reader_task, timeout=5)


if __name__ == "__main__":
    asyncio.run(_run_test())
