"""WebSocket endpoint for live session updates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import redis.asyncio as redis
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from core.types import TaskStatus

router = APIRouter()

WEBSOCKET_CHANNEL_TEMPLATE = "ws:broadcast:{session_id}"
HEARTBEAT_INTERVAL = 15


class ConnectionManager:
    """Manages active WebSocket connections per session."""

    def __init__(self) -> None:
        """Initialize internal connection registries."""

        self._connections: Dict[str, Set[WebSocket]] = {}
        self._listeners: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""

        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(session_id, set()).add(websocket)

    async def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        """Remove a WebSocket connection and stop listeners if empty."""

        async with self._lock:
            connections = self._connections.get(session_id)
            if connections and websocket in connections:
                connections.remove(websocket)
            if connections:
                return
            self._connections.pop(session_id, None)
            listener = self._listeners.pop(session_id, None)
            if listener:
                listener.cancel()

    async def broadcast(self, session_id: str, message: Dict[str, Any]) -> None:
        """Broadcast a message to all connected clients for a session."""

        async with self._lock:
            targets = list(self._connections.get(session_id, set()))

        for websocket in targets:
            try:
                await websocket.send_json(message)
            except Exception:
                await self.disconnect(session_id, websocket)

    async def ensure_listener(self, session_id: str, redis_client: redis.Redis) -> None:
        """Ensure a Redis subscription listener is running for the session."""

        async with self._lock:
            if session_id in self._listeners:
                return
            task = asyncio.create_task(self._listen(session_id, redis_client))
            self._listeners[session_id] = task

    async def _listen(self, session_id: str, redis_client: redis.Redis) -> None:
        """Subscribe to Redis broadcasts and forward to WebSocket clients."""

        channel = WEBSOCKET_CHANNEL_TEMPLATE.format(session_id=session_id)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                try:
                    payload = json.loads(raw.get("data", "{}"))
                except json.JSONDecodeError:
                    continue

                event = _format_event(payload)
                if event:
                    await self.broadcast(session_id, event)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()


manager = ConnectionManager()


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, request: Request) -> None:
    """Stream session events to the WebSocket client."""

    await manager.connect(session_id, websocket)
    await manager.ensure_listener(session_id, request.app.state.redis)

    try:
        status = await request.app.state.orchestrator.get_session_status(session_id)
        await websocket.send_json({"event": "session_state", "data": status})

        heartbeat_task = asyncio.create_task(_heartbeat(websocket))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        await manager.disconnect(session_id, websocket)


async def _heartbeat(websocket: WebSocket) -> None:
    """Send periodic ping messages to keep the connection alive."""

    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await websocket.send_json({"event": "ping"})
    except Exception:
        return


def _format_event(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize AgentMessage payloads into frontend event format."""

    agent_type = message.get("from_agent")
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}

    task_id = payload.get("task_id")
    content = None
    if "chunk" in payload and isinstance(payload.get("chunk"), str):
        content = payload.get("chunk")
    elif "content" in payload and isinstance(payload.get("content"), str):
        content = payload.get("content")
    else:
        content = json.dumps(payload) if payload else None

    return {
        "event": "agent_update",
        "agent_type": agent_type,
        "task_id": task_id,
        "status": message.get("status"),
        "content": content,
        "confidence": message.get("confidence"),
        "timestamp": message.get("timestamp"),
    }
