import base64

import pytest

from api_summary.store import ServiceError, Store


def uploaded(store, owner="alice", data=b"hello", name="source.txt"):
    key = store.begin(owner, name, len(data))["upload_id"]
    for index, offset in enumerate(range(0, len(data), store.s.max_chunk_bytes)):
        store.chunk(
            owner, key, index, base64.b64encode(data[offset : offset + store.s.max_chunk_bytes]).decode()
        )
    return store.finish(owner, key)["file_id"]


def test_chunks_idempotent_order_and_ownership(store):
    key = store.begin("alice", "dossier.txt", 5)["upload_id"]
    block = base64.b64encode(b"hello").decode()
    with pytest.raises(ServiceError):
        store.chunk("bob", key, 0, block)
    with pytest.raises(ServiceError):
        store.chunk("alice", key, 1, block)
    store.chunk("alice", key, 0, block)
    store.chunk("alice", key, 0, block)
    with pytest.raises(ServiceError):
        store.chunk("alice", key, 0, base64.b64encode(b"other").decode())
    assert store.finish("alice", key)["file_id"] == key
    assert store.path(key).read_bytes() == b"hello"


def test_jobs_private_durable_and_deduplicated(store, settings):
    key = uploaded(store)
    job = store.start("alice", [key], "Résume")["job_id"]
    assert store.start("alice", [key], "Résume")["job_id"] == job
    with pytest.raises(ServiceError):
        store.start("bob", [key], "Résume")
    with pytest.raises(ServiceError):
        store.status("bob", job)
    with pytest.raises(ServiceError):
        store.result("alice", job)
    assert store.claim()["id"] == job
    reopened = Store(settings)
    reopened.recover()
    assert reopened.status("alice", job)["status"] == "pending"
    reopened.complete(job, "Résumé final")
    assert reopened.result("alice", job) == "Résumé final"
    with pytest.raises(ServiceError):
        reopened.result("bob", job)


def test_quotas_and_incomplete(store, settings):
    settings.max_owner_bytes = 8
    key = store.begin("alice", "small.txt", 5)["upload_id"]
    with pytest.raises(ServiceError):
        store.begin("alice", "next.txt", 5)
    with pytest.raises(ServiceError):
        store.finish("alice", key)
    with pytest.raises(ServiceError):
        store.begin("alice", "script.exe", 1)
    with pytest.raises(ServiceError):
        store.begin("alice", "zero.txt", 0)
    with pytest.raises(ServiceError):
        store.chunk("alice", key, 0, "%%%")


def test_crash_append_is_reconciled(store):
    key = store.begin("alice", "a.txt", 5)["upload_id"]
    store.path(key).write_bytes(b"uncommitted bytes")
    store.chunk("alice", key, 0, base64.b64encode(b"hello").decode())
    store.finish("alice", key)
    assert store.path(key).read_bytes() == b"hello"


def test_cleanup_keeps_running_inputs(store):
    key = uploaded(store)
    job = store.start("alice", [key], "Résume")["job_id"]
    with store.db() as db:
        db.execute("UPDATE files SET created=0")
    store.cleanup()
    assert store.path(key).exists()
    store.complete(job, "done")
    with store.db() as db:
        db.execute("UPDATE jobs SET updated=0")
    store.cleanup()
    assert not store.path(key).exists()
    with pytest.raises(ServiceError):
        store.status("alice", job)
