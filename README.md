# 🌾 AI Harvest Agent

An AI-powered web harvesting platform that combines **Claude** (Anthropic) with **Playwright** browser automation to intelligently extract structured data from any website.

## Architecture

```
ai-harvest-agent/
├── app/
│   ├── main.py              ← FastAPI application entry point
│   ├── config.py            ← Settings (pydantic-settings)
│   ├── agents/              ← AI agent logic
│   │   ├── base_agent.py    ← Abstract agent base class
│   │   ├── harvest_agent.py ← Orchestrates LLM + Playwright
│   │   ├── scraper_agent.py ← Structured extraction specialist
│   │   └── orchestrator.py  ← Multi-agent coordinator
│   ├── services/            ← Business logic
│   │   ├── playwright_service.py ← Browser pool + page actions
│   │   ├── llm_service.py        ← Anthropic Claude wrapper
│   │   ├── harvest_service.py    ← Job management
│   │   ├── storage_service.py    ← Results persistence
│   │   └── queue_service.py      ← Celery task queue
│   ├── prompts/             ← Prompt templates
│   ├── models/              ← Pydantic + SQLAlchemy models
│   ├── routes/              ← FastAPI routers
│   └── core/                ← Deps, exceptions, middleware
├── tests/
├── docker-compose.yml
└── Dockerfile
```

## Quick Start

```bash
# 1. Clone and enter directory
cd ai-harvest-agent

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 4. Configure environment
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY at minimum

# 5. Start services (Docker)
docker-compose up -d postgres redis

# 6. Run database migrations
alembic upgrade head

# 7. Start the API
uvicorn app.main:app --reload
```

API is now live at `http://localhost:8000`  
Interactive docs: `http://localhost:8000/docs`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/harvest/start` | Start a new harvest job |
| `GET`  | `/api/v1/harvest/{job_id}` | Get job status |
| `GET`  | `/api/v1/harvest/{job_id}/results` | Get harvested results |
| `DELETE` | `/api/v1/harvest/{job_id}` | Cancel / delete job |
| `GET`  | `/api/v1/agents` | List available agents |
| `POST` | `/api/v1/agents/run` | Run an agent directly |
| `GET`  | `/api/v1/tasks` | List all tasks |
| `GET`  | `/api/v1/tasks/{task_id}` | Get task details |
| `GET`  | `/health` | Health check |

## Example: Start a Harvest

```bash
curl -X POST http://localhost:8000/api/v1/harvest/start \
  -H "X-API-Key: your-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/products",
    "goal": "Extract all product names, prices, and descriptions",
    "agent_type": "harvest",
    "max_pages": 5,
    "output_schema": {
      "products": [{"name": "string", "price": "number", "description": "string"}]
    }
  }'
```

## Running Tests

```bash
pytest tests/ -v
```

## Environment Variables

See [.env.example](.env.example) for full configuration reference.
