"""Local integration fixture: fake summary server, real Streamable HTTP MCP."""

import asyncio
import base64
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

from openwebui.tool import BridgeError, Tools


class FixtureTools(Tools):
    # Exercise real MCP transport; production authentication is tested separately.
    @asynccontextmanager
    async def _session(self, request, user_data, metadata=None, event_call=None):
        async with streamablehttp_client(self.test_url) as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                yield session


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.uploads = {}
        cls.completed = {}
        cls.jobs = {}
        server = FastMCP("fixture", stateless_http=True, json_response=True)

        @server.tool()
        def upload_begin(filename: str, size_bytes: int) -> dict:
            key = str(uuid.uuid4())
            cls.uploads[key] = {"name": filename, "size": size_bytes, "chunks": []}
            return {"upload_id": key}

        @server.tool()
        def upload_chunk(upload_id: str, index: int, data_base64: str) -> dict:
            entry = cls.uploads[upload_id]
            assert index == len(entry["chunks"])
            entry["chunks"].append(base64.b64decode(data_base64, validate=True))
            return {"ok": True}

        @server.tool()
        def upload_finish(upload_id: str) -> dict:
            entry = cls.uploads.pop(upload_id)
            data = b"".join(entry["chunks"])
            assert len(data) == entry["size"]
            cls.completed[upload_id] = {"name": entry["name"], "data": data}
            return {"file_id": upload_id}

        @server.tool()
        def start_summary(file_ids: list[str], instructions: str) -> dict:
            assert instructions == "Résumé test"
            key = str(uuid.uuid4())
            cls.jobs[key] = {"status": "running", "file_ids": file_ids}
            return {"job_id": key}

        @server.tool()
        def get_status(job_id: str) -> dict:
            return {"job_id": job_id, "status": cls.jobs[job_id]["status"]}

        @server.tool()
        def get_result(job_id: str) -> str:
            entry = cls.jobs[job_id]
            assert entry["status"] == "completed"
            return "Résumé simulé : " + ", ".join(cls.completed[k]["name"] for k in entry["file_ids"])

        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        port = cls.sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}/mcp"
        cls.server = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Fixture failed to start")
            time.sleep(0.02)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.sock.close()

    async def test_originals_pdf_zip_and_mcp_summary(self):
        tool = FixtureTools()
        tool.test_url = self.url
        tool.valves.OPENWEBUI_URL = "http://owui.test"
        pdf_id, zip_id = str(uuid.uuid4()), str(uuid.uuid4())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("document.txt", "contenu original")
        originals = {pdf_id: b"%PDF-1.7\n" + bytes(range(256)) * 2500, zip_id: buf.getvalue()}
        names = {pdf_id: "document.pdf", zip_id: "archive.zip"}
        real_client = httpx.AsyncClient

        def download(request):
            self.assertEqual(request.headers["authorization"], "Bearer user-only")
            file_id = request.url.path.split("/")[-2]
            return httpx.Response(200, content=originals[file_id])

        def client(*args, **kwargs):
            return real_client(*args, **kwargs, transport=httpx.MockTransport(download))

        events = []

        async def emit(event):
            events.append(event)

        # Local download is mocked; the MCP SDK uses its real HTTP client factory.
        with tempfile.TemporaryDirectory() as tmp:
            tool.valves.TEMP_DIR = tmp
            with patch.object(tool, "_local_client", side_effect=client):
                result = await tool.summarize_attachments(
                    "Résumé test",
                    [{"id": k, "name": names[k]} for k in originals],
                    SimpleNamespace(headers={"authorization": "Bearer user-only"}),
                    emit,
                )
            self.assertEqual(await asyncio.to_thread(lambda: list(Path(tmp).iterdir())), [])
        submitted = json.loads(result)
        self.assertEqual(submitted["status"], "submitted")
        job_id = submitted["job_id"]
        # Subsequent calls reopen a session and require no attachments/download.
        request = SimpleNamespace(headers={"authorization": "Bearer user-only"})
        pending = json.loads(await tool.get_summary_result(job_id, __request__=request))
        self.assertEqual(pending["status"], "running")
        self.assertNotIn("summary", pending)
        self.jobs[job_id]["status"] = "completed"
        ready = json.loads(await tool.get_summary_status(job_id, __request__=request))
        self.assertEqual(ready["status"], "completed")
        final = json.loads(await tool.get_summary_result(job_id, __request__=request))
        self.assertIn("Résumé simulé", final["summary"])
        self.assertEqual(len(self.jobs[job_id]["file_ids"]), 2)
        for key, name in names.items():
            transferred = next(v for v in self.completed.values() if v["name"] == name)
            self.assertEqual(transferred["data"], originals[key])
        self.assertTrue(events[-1]["data"]["done"])

    async def test_failed_and_cancelled_do_not_fetch_result(self):
        tool = FixtureTools()
        tool.test_url = self.url
        for state in ("failed", "cancelled", "pending"):
            key = str(uuid.uuid4())
            self.jobs[key] = {"status": state, "file_ids": []}
            result = json.loads(await tool.get_summary_result(key, __request__=object()))
            self.assertEqual(result["status"], state)
            self.assertNotIn("summary", result)

    async def test_bad_job_and_unknown_job(self):
        tool = FixtureTools()
        tool.test_url = self.url
        self.assertIn("error", json.loads(await tool.get_summary_status("", __request__=object())))
        unknown = json.loads(await tool.get_summary_result("unknown", __request__=object()))
        self.assertIn("error", unknown)
        self.assertNotIn("summary", unknown)

    async def test_size_limit_and_cleanup(self):
        tool = FixtureTools()
        tool.test_url = self.url
        tool.valves.MAX_FILE_MIB = 1
        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            return real_client(
                *args,
                **kwargs,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b"x" * (1024**2 + 1))
                ),
            )

        with tempfile.TemporaryDirectory() as tmp:
            tool.valves.TEMP_DIR = tmp
            with patch.object(tool, "_local_client", side_effect=client):
                result = await tool.summarize_attachments(
                    __files__=[{"id": str(uuid.uuid4())}],
                    __request__=SimpleNamespace(headers={"authorization": "Bearer user-only"}),
                )
            self.assertIn("Limite de taille", result)
            self.assertEqual(await asyncio.to_thread(lambda: list(Path(tmp).iterdir())), [])

    async def test_no_attachment_and_no_auth(self):
        tool = FixtureTools()
        self.assertIn("Joignez", await tool.summarize_attachments())
        self.assertIn(
            "Authentification",
            await tool.summarize_attachments(
                __files__=[{"id": str(uuid.uuid4())}], __request__=SimpleNamespace(headers={})
            ),
        )

    def test_duplicate_files_and_url_rejection(self):
        tool = FixtureTools()
        key = str(uuid.uuid4())
        self.assertEqual(len(tool._attachments([{"id": key}, {"file": {"id": key}}])), 1)
        with self.assertRaises(ValueError):
            tool._attachments([{"id": "https://external.invalid/file"}])


class RegisteredSSOTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, auth_type="oauth_2.1_static", resolved=True):
        tool = Tools()
        tool.valves.MCP_SERVER_ID = "summary"
        user = SimpleNamespace(id="alice")
        users = SimpleNamespace(get_user_by_id=AsyncMock(return_value=user))
        config = SimpleNamespace(
            get=AsyncMock(
                return_value=[
                    {
                        "type": "mcp",
                        "info": {"id": "summary"},
                        "auth_type": auth_type,
                    }
                ]
            )
        )
        client = SimpleNamespace(session=object(), disconnect=AsyncMock())
        names = ["upload_begin", "upload_chunk", "upload_finish", "start_summary", "get_status", "get_result"]
        connector = AsyncMock(return_value=(client, [{"name": n} for n in names]) if resolved else None)
        modules = {
            "open_webui.models.config": SimpleNamespace(Config=config),
            "open_webui.models.users": SimpleNamespace(Users=users),
            "open_webui.utils.tools": SimpleNamespace(connect_mcp_server=connector),
        }
        return tool, user, client, connector, modules

    async def test_reuses_each_registered_oauth_mode_and_user(self):
        for mode in ("system_oauth", "oauth_2.1", "oauth_2.1_static"):
            tool, user, client, connector, modules = self.fixture(mode)
            request = object()
            with patch.dict(sys.modules, modules):
                async with tool._session(request, {"id": "alice"}, {"chat_id": "chat"}) as session:
                    self.assertIs(session, client.session)
            connector.assert_awaited_once_with(
                request,
                "summary",
                user,
                {
                    "__metadata__": {"chat_id": "chat"},
                    "__event_call__": None,
                },
            )
            client.disconnect.assert_awaited_once()

    async def test_no_shared_bearer_fallback(self):
        tool, _, _, connector, modules = self.fixture("bearer")
        with patch.dict(sys.modules, modules):
            with self.assertRaises(BridgeError):
                async with tool._session(object(), {"id": "alice"}):
                    self.fail("must refuse shared bearer")
        connector.assert_not_awaited()

    async def test_access_denied_stays_denied(self):
        tool, _, _, _, modules = self.fixture(resolved=False)
        with patch.dict(sys.modules, modules):
            with self.assertRaises(BridgeError):
                async with tool._session(object(), {"id": "alice"}):
                    self.fail("must refuse access")


if __name__ == "__main__":
    unittest.main()
