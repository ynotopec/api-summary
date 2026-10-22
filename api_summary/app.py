import asyncio
import fcntl
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from .auth import Authenticator, SecurityMiddleware, current_owner
from .config import Settings
from .engine import Summarizer
from .store import ServiceError, Store
from .worker import Worker


class Begin(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(gt=0)


class Chunk(BaseModel):
    index: int = Field(ge=0)
    data_base64: str


class Start(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=100)
    instructions: str = Field(default="Résume en français les documents et cite leurs noms.", max_length=8000)


def create_app(settings: Settings | None = None, *, engine=None, authenticator=None, start_worker=True):
    s = settings or Settings()
    store = Store(s)
    auth = authenticator or Authenticator(s)
    mcp = FastMCP("api-summary", host="0.0.0.0", stateless_http=True, json_response=True)

    async def call(method, *args):
        return await asyncio.to_thread(method, current_owner(), *args)

    @mcp.tool()
    async def upload_begin(filename: str, size_bytes: int) -> dict:
        """Reserve a private file upload. Transfer bytes with upload_chunk, never a file URL."""
        return await call(store.begin, filename, size_bytes)

    @mcp.tool()
    async def upload_chunk(upload_id: str, index: int, data_base64: str) -> dict:
        """Append one ordered base64 block; replaying identical blocks is idempotent."""
        return await call(store.chunk, upload_id, index, data_base64)

    @mcp.tool()
    async def upload_finish(upload_id: str) -> dict:
        """Validate complete bytes and obtain a private file_id."""
        return await call(store.finish, upload_id)

    @mcp.tool()
    async def start_summary(
        file_ids: list[str], instructions: str = "Résume en français les documents et cite leurs noms."
    ) -> dict:
        """Queue a durable background summary; returns job_id immediately, not a summary."""
        return await call(store.start, file_ids, instructions)

    @mcp.tool()
    async def get_status(job_id: str) -> dict:
        """Read a private job's status once. Do not poll in a tight loop."""
        return await call(store.status, job_id)

    @mcp.tool()
    async def get_result(job_id: str) -> str:
        """Read a completed summary. Does not wait or restart the job."""
        return await call(store.result, job_id)

    transport = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        lock = (s.data_dir / ".instance.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("One process/replica per data volume is required") from None
        summarizer = None
        task = None
        try:
            await auth.initialize()
            summarizer = engine or Summarizer(s)
            app.state.worker = Worker(store, summarizer)
            async with mcp.session_manager.run():
                if start_worker:
                    task = asyncio.create_task(app.state.worker.run(), name="summary-worker")
                app.state.worker_task = task
                try:
                    yield
                finally:
                    if task:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
        finally:
            if summarizer:
                await summarizer.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    app = FastAPI(title="api-summary", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(SecurityMiddleware, authenticator=auth)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/healthz")
    async def health():
        task = getattr(app.state, "worker_task", None)
        if start_worker and (task is None or task.done()):
            return JSONResponse({"status": "worker-unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def resource_metadata():
        return {
            "resource": s.public_base_url.rstrip("/") + "/mcp",
            "authorization_servers": [s.oidc_issuer] if s.oidc_issuer else [],
            "scopes_supported": s.oidc_scopes.split(),
            "bearer_methods_supported": ["header"],
        }

    @app.post("/v1/uploads", status_code=201)
    async def begin(body: Begin):
        return await upload_begin(body.filename, body.size_bytes)

    @app.put("/v1/uploads/{upload_id}/chunks")
    async def chunk(upload_id: str, body: Chunk):
        return await upload_chunk(upload_id, body.index, body.data_base64)

    @app.post("/v1/uploads/{upload_id}/finish")
    async def finish(upload_id: str):
        return await upload_finish(upload_id)

    @app.post("/v1/summaries", status_code=202)
    async def start(body: Start):
        return await start_summary(body.file_ids, body.instructions)

    @app.get("/v1/summaries/{job_id}")
    async def status(job_id: str):
        return await get_status(job_id)

    @app.get("/v1/summaries/{job_id}/result", response_class=PlainTextResponse)
    async def result(job_id: str):
        return await get_result(job_id)

    # The SDK owns /mcp, supporting JSON responses and Streamable HTTP.
    app.mount("/", transport)
    return app
