from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent_service.api import chat, documents, health, ingest, sessions
from agent_service.db.pool import close_pool, get_pool

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()  # warm
    yield
    await close_pool()


app = FastAPI(title="Agent Service", version="0.1.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(ingest.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(sessions.router, prefix="/api/v1")

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
