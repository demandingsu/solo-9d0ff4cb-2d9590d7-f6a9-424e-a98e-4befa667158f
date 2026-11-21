"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import APP_NAME
from .templating import templates
from .version import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # pragma: no cover - 路由兜底
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "资源不存在"}, status_code=404)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"code": 404, "message": "页面或资源不存在", "active": ""},
            status_code=404,
        )

    from .routes import router, api_router

    app.include_router(router)
    app.include_router(api_router, prefix="/api")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
