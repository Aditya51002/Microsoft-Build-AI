# ResearchSwarm

**ResearchSwarm is a trust-first multi-agent research copilot for decision briefs.**

It turns a broad question into a source-backed report by coordinating five specialist agents:
Planner, Researcher, Analyst, Critic, and Writer. The differentiator is the visible trust layer:
every run exposes agent status, claim-level evidence, confidence, critic notes, retries, and a final
markdown brief.

## Why It Fits The Hackathon

| Evaluation Area | How ResearchSwarm Addresses It |
| --- | --- |
| AI Integration & Intelligence Design | Five role-specific agents with decomposition, evidence gathering, synthesis, adversarial critique, and final writing. |
| System Architecture & Engineering Quality | FastAPI, Redis pub/sub, typed Pydantic schemas, task DAG orchestration, retries, timeouts, WebSocket streaming, Docker Compose. |
| Communication, Presentation & UX | Live dashboard with agent pipeline, writer stream, trust ledger, confidence, critic notes, and final report. |
| Prototype Readiness & Scalability | One-command Docker startup, deterministic demo fallback, Redis-backed state, replay/record endpoints. |
| Problem Depth & Product Clarity | Built for high-stakes research decisions where teams need traceable evidence rather than opaque chat answers. |
| Market Understanding & Product Fit | Targets founders, analysts, students, and product teams preparing decision memos, market scans, and technical due diligence. |

## Architecture

```text
User Query
   |
   v
Planner -> Researcher -> Analyst -> Critic -> Writer
   |          |            |          |         |
   +----------+------------+----------+---------+
              Redis pub/sub + Task DAG
                         |
                         v
               Live WebSocket Dashboard
```

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Open the dashboard:

```text
http://localhost:3000
```

Backend health:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/health
```

## Demo Mode

ResearchSwarm can run without an Anthropic key using deterministic demo output. This keeps the
prototype reliable for judging and local review.

```env
RESEARCHSWARM_DEMO_MODE=true
VITE_DEMO_MODE=true
```

For live model calls, set:

```env
ANTHROPIC_API_KEY=your_key_here
RESEARCHSWARM_DEMO_MODE=false
```

## API Highlights

- `POST /api/sessions` starts a research run.
- `GET /api/sessions/{session_id}` returns task DAG status.
- `GET /api/sessions/{session_id}/report` returns the final report, sources, confidence, critic notes, and claim ledger.
- `WS /ws/{session_id}` streams live agent updates.
- `POST /api/demo/record/{session_id}` records a completed run.
- `POST /api/demo/replay/{name}` replays a saved run.

## Local Development

Backend:

```bash
cd backend
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

## Suggested Demo Prompt

```text
Should an early-stage startup build a source-backed AI research copilot for product and market due diligence?
```

The judge-visible story: ResearchSwarm does not just answer. It plans, researches, synthesizes,
criticizes weak claims, exposes confidence, and writes a decision brief.
