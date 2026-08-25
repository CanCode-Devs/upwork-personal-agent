from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

import app.tools.discovery  # noqa: F401
import app.tools.execution  # noqa: F401
import app.tools.memory  # noqa: F401
from app.auth import SessionMiddleware, bootstrap_user, is_public_path
from app.config import get_settings
from app.db.session import SessionLocal, init_db
from app.runtime import get_or_create_runtime
from app.web.routes import router
from app.worker import poll_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if is_public_path(request.url.path):
            return await call_next(request)
        if getattr(request.state, "username", None):
            return await call_next(request)
        return RedirectResponse("/login", status_code=303)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db()
    db = SessionLocal()
    try:
        bootstrap_user(db, settings)
        get_or_create_runtime(db, settings)
        try:
            from app.memory_seed import seed_agent_case_studies

            await seed_agent_case_studies(db)
        except Exception:
            logging.getLogger(__name__).exception("Agent case study seed failed")
        db.commit()
    finally:
        db.close()
    task = asyncio.create_task(poll_loop(settings))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Upwork Job Watcher", lifespan=lifespan)
    app.add_middleware(AuthGateMiddleware)
    app.add_middleware(SessionMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
    app.include_router(router)

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


app = create_app()
