from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from kb_assistant.errors import AppError


def spa_router(dist: Path) -> APIRouter:
    """Serves the built frontend, with index.html for client-side routes like /callback."""
    router = APIRouter()
    root = dist.resolve()

    @router.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("v1/") or path == "healthz":
            raise AppError("Not found", 404)
        candidate = (root / path).resolve()
        if path and candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(root / "index.html", headers={"cache-control": "no-cache"})

    return router
