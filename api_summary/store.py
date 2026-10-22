import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import Settings


class ServiceError(ValueError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        (self.root / "files").mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "state.sqlite3"
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    expected INTEGER NOT NULL, received INTEGER NOT NULL DEFAULT 0,
                    next_chunk INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    file_id TEXT NOT NULL, idx INTEGER NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(file_id,idx));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, file_ids TEXT NOT NULL,
                    instructions TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    fingerprint TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS job_owner ON jobs(owner,status);
                CREATE UNIQUE INDEX IF NOT EXISTS dedupe_job ON jobs(owner,fingerprint);
            """)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.db_path, timeout=30)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                with db:
                    yield db
            finally:
                db.close()

    def path(self, file_id: str) -> Path:
        # IDs read from the database are always generated here, never user paths.
        return self.root / "files" / str(uuid.UUID(file_id))

    def owned_file(self, db, owner, file_id, ready=False):
        row = db.execute("SELECT * FROM files WHERE id=? AND owner=?", (file_id, owner)).fetchone()
        if row is None or (ready and not row["ready"]):
            raise ServiceError("File not found or not ready")
        return row

    def begin(self, owner: str, filename: str, size_bytes: int):
        if type(size_bytes) is not int or not 0 < size_bytes <= self.s.max_file_bytes:
            raise ServiceError("Invalid file size")
        name = filename.replace("\\", "/").split("/")[-1]
        if not name or len(name) > 200 or any(not c.isprintable() for c in name):
            raise ServiceError("Invalid filename")
        if Path(name).suffix.lower() not in {".pdf", ".txt", ".md", ".csv", ".json", ".docx", ".zip"}:
            raise ServiceError("Unsupported file type")
        key = str(uuid.uuid4())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            reserved = db.execute("SELECT COALESCE(SUM(expected),0) FROM files").fetchone()[0]
            owned = db.execute(
                "SELECT COALESCE(SUM(expected),0) FROM files WHERE owner=?", (owner,)
            ).fetchone()[0]
            if (
                reserved + size_bytes > self.s.max_storage_bytes
                or owned + size_bytes > self.s.max_owner_bytes
            ):
                raise ServiceError("Storage quota exceeded")
            db.execute(
                "INSERT INTO files(id,owner,name,expected,created) VALUES(?,?,?,?,?)",
                (key, owner, name, size_bytes, time.time()),
            )
            with self.path(key).open("xb"):
                pass
        return {"upload_id": key}

    def chunk(self, owner: str, upload_id: str, index: int, data_base64: str):
        if type(index) is not int or index < 0 or len(data_base64) > 4 * ((self.s.max_chunk_bytes + 2) // 3):
            raise ServiceError("Invalid block size or index")
        try:
            chunk = base64.b64decode(data_base64, validate=True)
        except (ValueError, TypeError):
            raise ServiceError("Invalid base64") from None
        if not chunk or len(chunk) > self.s.max_chunk_bytes:
            raise ServiceError("Invalid block size")
        digest = hashlib.sha256(chunk).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned_file(db, owner, upload_id)
            previous = db.execute(
                "SELECT digest FROM chunks WHERE file_id=? AND idx=?", (upload_id, index)
            ).fetchone()
            if previous is not None:
                if previous[0] != digest:
                    raise ServiceError("Conflicting block retry")
                return {"ok": True, "next_index": row["next_chunk"]}
            if row["ready"] or index != row["next_chunk"] or row["received"] + len(chunk) > row["expected"]:
                raise ServiceError("Unexpected block or file size exceeded")
            with self.path(row["id"]).open("r+b") as output:
                # Recover an append written before a process crash/DB commit.
                output.truncate(row["received"])
                output.seek(row["received"])
                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            db.execute("INSERT INTO chunks VALUES(?,?,?)", (upload_id, index, digest))
            db.execute(
                "UPDATE files SET received=received+?,next_chunk=next_chunk+1 WHERE id=?",
                (len(chunk), upload_id),
            )
        return {"ok": True, "next_index": index + 1}

    def finish(self, owner: str, upload_id: str):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned_file(db, owner, upload_id)
            if row["received"] != row["expected"]:
                raise ServiceError("File is incomplete")
            if self.path(row["id"]).stat().st_size != row["expected"]:
                raise ServiceError("Stored file size mismatch")
            db.execute("UPDATE files SET ready=1 WHERE id=?", (upload_id,))
        return {"file_id": upload_id}

    def start(self, owner: str, file_ids: list[str], instructions: str):
        file_ids = list(dict.fromkeys(file_ids))
        if not file_ids or len(file_ids) > self.s.max_files or len(instructions) > 8000:
            raise ServiceError("Invalid files or instructions")
        fingerprint = hashlib.sha256(json.dumps([sorted(file_ids), instructions]).encode()).hexdigest()
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for key in file_ids:
                self.owned_file(db, owner, key, ready=True)
            previous = db.execute(
                "SELECT id FROM jobs WHERE owner=? AND fingerprint=?", (owner, fingerprint)
            ).fetchone()
            if previous:
                return {"job_id": previous[0]}
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[
                0
            ]
            owned = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN ('pending','running')", (owner,)
            ).fetchone()[0]
            if queued >= self.s.max_queued_jobs or owned >= self.s.max_jobs_per_owner:
                raise ServiceError("Job queue quota exceeded")
            key = str(uuid.uuid4())
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    owner,
                    json.dumps(file_ids),
                    instructions,
                    "pending",
                    None,
                    None,
                    now,
                    now,
                    fingerprint,
                ),
            )
        return {"job_id": key}

    def job(self, owner: str, job_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if row is None:
            raise ServiceError("Job not found")
        return dict(row)

    def status(self, owner: str, job_id: str):
        row = self.job(owner, job_id)
        return {"job_id": job_id, "status": row["status"], "error": row["error"]}

    def result(self, owner: str, job_id: str):
        row = self.job(owner, job_id)
        if row["status"] != "completed":
            raise ServiceError("Job has not completed")
        return row["result"]

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='pending',updated=? WHERE status='running'", (time.time(),))

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def complete(self, job_id, result=None, error=None):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?",
                ("failed" if error else "completed", result, error, time.time(), job_id),
            )

    def inputs(self, job):
        with self.db() as db:
            return [
                (self.path(row["id"]), row["name"])
                for key in json.loads(job["file_ids"])
                for row in [self.owned_file(db, job["owner"], key, ready=True)]
            ]

    def cleanup(self):
        cutoff = time.time() - self.s.retention_seconds
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM jobs WHERE status IN ('completed','failed') AND updated<?", (cutoff,))
            # Keep files referenced by any retained job, including queued/running jobs.
            referenced = {
                key for row in db.execute("SELECT file_ids FROM jobs") for key in json.loads(row[0])
            }
            for row in db.execute("SELECT id FROM files WHERE created<?", (cutoff,)).fetchall():
                if row["id"] not in referenced:
                    self.path(row["id"]).unlink(missing_ok=True)
                    db.execute("DELETE FROM chunks WHERE file_id=?", (row["id"],))
                    db.execute("DELETE FROM files WHERE id=?", (row["id"],))
            # Reap files created before an uncommitted begin crashed.
            known = {row[0] for row in db.execute("SELECT id FROM files")}
            for path in (self.root / "files").iterdir():
                if path.name not in known and path.stat().st_mtime < cutoff:
                    path.unlink()
