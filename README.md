# ResearchSwarm

ResearchSwarm is a multi-agent AI research system scaffold with a FastAPI backend, React/Vite frontend, and Redis for shared state and coordination.

## Services

- Backend: FastAPI on `http://localhost:8000`
- Frontend: React 18 + TypeScript + Vite on `http://localhost:3000`
- Redis: `localhost:6379`

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

The backend health check is available at:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API key used by research agents. | Empty |
| `REDIS_URL` | Redis connection URL used by the backend. | `redis://redis:6379/0` |
| `CORS_ORIGIN` | Allowed frontend origin for browser requests. | `http://localhost:3000` |

## Local Development Without Docker

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
npm install
npm run dev
```
