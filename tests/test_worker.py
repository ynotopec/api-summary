import asyncio

from test_store import uploaded

from api_summary.worker import Worker


class FailingEngine:
    async def summarize(self, docs, instructions):
        raise RuntimeError("private provider details must not escape")


async def test_job_failure_recorded_without_sensitive_exception(store):
    key = uploaded(store)
    job = store.start("alice", [key], "Résume")["job_id"]
    worker = Worker(store, FailingEngine())
    assert await worker.run_once()
    state = store.status("alice", job)
    assert state["status"] == "failed"
    assert "private" not in state["error"]


async def test_job_timeout_is_durable_failure(store):
    class SlowEngine:
        async def summarize(self, docs, instructions):
            await asyncio.sleep(10)

    store.s.job_timeout = 0.1
    key = uploaded(store)
    job = store.start("alice", [key], "Résume")["job_id"]
    worker = Worker(store, SlowEngine())
    assert await worker.run_once()
    assert store.status("alice", job)["status"] == "failed"
