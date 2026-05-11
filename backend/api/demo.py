"""Demo mode endpoints for recordings and replay."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request

from core.schemas import ResearchQuery

router = APIRouter()

RECORDING_KEY_TEMPLATE = "demo:recording:{name}"
SESSION_EVENTS_TEMPLATE = "session:{session_id}:events"
WEBSOCKET_CHANNEL_TEMPLATE = "ws:broadcast:{session_id}"


@router.post("/api/demo/record/{session_id}")
async def record_session(
    session_id: str,
    request: Request,
    name: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Record WebSocket events from a completed session."""

    orchestrator = request.app.state.orchestrator
    status = await orchestrator.get_session_status(session_id)
    if status.get("status") == "unknown":
        raise HTTPException(status_code=404, detail="Session not found")
    if not status.get("complete"):
        raise HTTPException(status_code=409, detail="Session not completed")

    recording_name = name or session_id
    events_key = SESSION_EVENTS_TEMPLATE.format(session_id=session_id)
    raw_events = await request.app.state.redis.lrange(events_key, 0, -1)
    if not raw_events:
        raise HTTPException(status_code=404, detail="No events found for session")

    ordered = _build_recording(raw_events)
    recording_key = RECORDING_KEY_TEMPLATE.format(name=recording_name)
    await request.app.state.redis.set(recording_key, json.dumps(ordered))

    return {
        "name": recording_name,
        "event_count": len(ordered),
    }


@router.post("/api/demo/replay/{name}")
async def replay_recording(
    name: str,
    request: Request,
    speed: float = Query(default=1.0, ge=0.25, le=4.0),
) -> Dict[str, Any]:
    """Replay a recording through the WebSocket broadcast channel."""

    recording_key = RECORDING_KEY_TEMPLATE.format(name=name)
    raw = await request.app.state.redis.get(recording_key)
    if not raw:
        raise HTTPException(status_code=404, detail="Recording not found")

    recording = json.loads(raw)
    if not isinstance(recording, list):
        raise HTTPException(status_code=400, detail="Recording malformed")

    replay_session_id = str(uuid4())
    await request.app.state.redis.hset(
        f"session:{replay_session_id}:meta",
        mapping={
            "created_at": datetime.now(timezone.utc).isoformat(),
            "replay": "true",
            "recording": name,
        },
    )

    asyncio.create_task(
        _stream_recording(
            request,
            replay_session_id,
            recording,
            speed,
        )
    )

    return {"replay_session_id": replay_session_id}


@router.get("/api/demo/recordings")
async def list_recordings(request: Request) -> Dict[str, Any]:
    """List available demo recordings."""

    names: List[str] = []
    async for key in request.app.state.redis.scan_iter(match="demo:recording:*"):
        names.append(key.split("demo:recording:", 1)[-1])
    names.sort()
    return {"recordings": names}


@router.get("/api/demo/seed")
async def seed_demo(request: Request) -> Dict[str, Any]:
    """Run a demo session and store it as a recording."""

    orchestrator = request.app.state.orchestrator
    query = ResearchQuery(
        user_query="Market opportunity for solar energy in Southeast Asia",
        session_id=uuid4(),
    )

    session_id = await orchestrator.start_session(query)

    await _wait_for_completion(orchestrator, session_id)

    events_key = SESSION_EVENTS_TEMPLATE.format(session_id=session_id)
    raw_events = await request.app.state.redis.lrange(events_key, 0, -1)
    if not raw_events:
        raise HTTPException(status_code=500, detail="No events captured for demo")

    ordered = _build_recording(raw_events)
    recording_key = RECORDING_KEY_TEMPLATE.format(name="solar_energy_demo")
    await request.app.state.redis.set(recording_key, json.dumps(ordered))

    return {
        "session_id": session_id,
        "recording": "solar_energy_demo",
        "event_count": len(ordered),
    }


def _build_recording(raw_events: List[str]) -> List[Dict[str, Any]]:
    """Convert stored events into a list of {event, delay_ms} entries."""

    parsed: List[Dict[str, Any]] = []
    for entry in raw_events:
        try:
            parsed.append(json.loads(entry))
        except json.JSONDecodeError:
            continue

    parsed.sort(key=lambda item: item.get("timestamp_ms", 0))

    ordered: List[Dict[str, Any]] = []
    last_ts: Optional[int] = None
    for entry in parsed:
        timestamp = entry.get("timestamp_ms")
        event = entry.get("event")
        if not isinstance(timestamp, int) or not isinstance(event, dict):
            continue
        delay = 0 if last_ts is None else max(0, timestamp - last_ts)
        ordered.append({"event": event, "delay_ms": delay})
        last_ts = timestamp

    return ordered


async def _stream_recording(
    request: Request,
    session_id: str,
    recording: List[Dict[str, Any]],
    speed: float,
) -> None:
    """Replay events through the websocket broadcast channel."""

    channel = WEBSOCKET_CHANNEL_TEMPLATE.format(session_id=session_id)
    for entry in recording:
        delay_ms = float(entry.get("delay_ms", 0))
        delay = min(delay_ms / max(speed, 0.1), 500.0) / 1000
        await asyncio.sleep(delay)
        event = entry.get("event", {})
        if isinstance(event, dict):
            event = {**event, "timestamp": datetime.now(timezone.utc).isoformat()}
        await request.app.state.redis.publish(channel, json.dumps(event))


async def _wait_for_completion(orchestrator, session_id: str) -> None:
    """Poll session status until the run completes or times out."""

    deadline = time.time() + 300
    while time.time() < deadline:
        status = await orchestrator.get_session_status(session_id)
        if status.get("complete"):
            return
        await asyncio.sleep(2)
    raise HTTPException(status_code=504, detail="Demo seed timed out")
