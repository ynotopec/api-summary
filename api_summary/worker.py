import asyncio
import json
import logging
import os
import sys
import time

from .engine import Summarizer
from .store import ServiceError, Store

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, store: Store, engine: Summarizer):
        self.store = store
        self.engine = engine

    async def extract(self, job):
        fields = [
            "max_input_chars",
            "max_pdf_pages",
            "max_zip_entries",
            "max_zip_expanded_bytes",
            "max_zip_ratio",
        ]
        payload = {
            "files": [(str(path), name) for path, name in await asyncio.to_thread(self.store.inputs, job)],
            "limits": {key: getattr(self.store.s, key) for key in fields},
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "api_summary.extract",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                k: v for k, v in os.environ.items() if k in {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "TMPDIR"}
            },
        )
        try:
            async with asyncio.timeout(90):
                stdout, _ = await process.communicate(json.dumps(payload).encode())
            if process.returncode:
                raise ServiceError(
                    "Extraction failed: unsupported/empty/scanned document or extraction limit exceeded"
                )
            return json.loads(stdout)["documents"]
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def run_once(self):
        job = await asyncio.to_thread(self.store.claim)
        if job is None:
            return False
        try:
            async with asyncio.timeout(self.store.s.job_timeout):
                documents = await self.extract(job)
                summary = await self.engine.summarize(documents, job["instructions"])
            await asyncio.to_thread(self.store.complete, job["id"], summary)
        except asyncio.CancelledError:
            # Keep 'running': startup recovers this durable record to pending.
            raise
        except Exception as exc:
            # No document contents, tokens or provider response bodies in logs/results.
            log.warning("summary job %s failed (%s)", job["id"], type(exc).__name__)
            error = str(exc) if isinstance(exc, ServiceError) else "Processing failed or timed out"
            await asyncio.to_thread(self.store.complete, job["id"], error=error)
        return True

    async def run(self):
        await asyncio.to_thread(self.store.recover)
        last_cleanup = 0
        while True:
            if time.monotonic() - last_cleanup > 60:
                await asyncio.to_thread(self.store.cleanup)
                last_cleanup = time.monotonic()
            if not await self.run_once():
                await asyncio.sleep(0.5)
