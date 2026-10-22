import base64
import json
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from api_summary.app import create_app
from api_summary.auth import Authenticator


class FakeAuth(Authenticator):
    async def initialize(self):
        pass

    async def authenticate(self, token):
        if token not in {"alice", "bob"}:
            raise ValueError("invalid")
        return token


class FakeEngine:
    async def summarize(self, docs, instructions):
        return "Résumé : " + "; ".join(name + ": " + text for name, text in docs)

    async def close(self):
        pass


def test_rest_async_and_ownership(settings):
    settings.auth_enabled = True
    app = create_app(settings, authenticator=FakeAuth(settings), engine=FakeEngine(), start_worker=False)
    alice = {"Authorization": "Bearer alice"}
    bob = {"Authorization": "Bearer bob"}
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        denied = client.get("/v1/summaries/unknown")
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
        content = b"Information administrative importante."
        key = client.post(
            "/v1/uploads", headers=alice, json={"filename": "a.txt", "size_bytes": len(content)}
        ).json()["upload_id"]
        assert (
            client.put(
                f"/v1/uploads/{key}/chunks",
                headers=bob,
                json={"index": 0, "data_base64": base64.b64encode(content).decode()},
            ).status_code
            == 400
        )
        assert (
            client.put(
                f"/v1/uploads/{key}/chunks",
                headers=alice,
                json={"index": 0, "data_base64": base64.b64encode(content).decode()},
            ).status_code
            == 200
        )
        file_id = client.post(f"/v1/uploads/{key}/finish", headers=alice).json()["file_id"]
        response = client.post(
            "/v1/summaries", headers=alice, json={"file_ids": [file_id], "instructions": "Résume"}
        )
        assert response.status_code == 202
        job = response.json()["job_id"]
        assert client.get(f"/v1/summaries/{job}", headers=alice).json()["status"] == "pending"
        assert client.get(f"/v1/summaries/{job}/result", headers=alice).status_code == 400
        # Worker runs actual subprocess extraction; response submission did not execute the job.
        assert client.portal.call(app.state.worker.run_once)
        assert client.get(f"/v1/summaries/{job}", headers=alice).json()["status"] == "completed"
        assert "Information administrative" in client.get(f"/v1/summaries/{job}/result", headers=alice).text
        assert client.get(f"/v1/summaries/{job}", headers=bob).status_code == 400
        assert client.get(f"/v1/summaries/{job}/result", headers=bob).status_code == 400
        large = client.post("/v1/uploads", headers=alice, content=b"x" * 100000)
        assert large.status_code == 413


async def test_mcp_tools_real_sdk_and_identity(settings):
    settings.auth_enabled = True
    app = create_app(settings, authenticator=FakeAuth(settings), engine=FakeEngine(), start_worker=False)
    async with app.router.lifespan_context(app):

        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth
            )

        async def connection(token):
            return streamablehttp_client(
                "http://testserver/mcp",
                headers={"Authorization": f"Bearer {token}"},
                httpx_client_factory=factory,
            )

        async with await connection("alice") as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=10)
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {
                    "upload_begin",
                    "upload_chunk",
                    "upload_finish",
                    "start_summary",
                    "get_status",
                    "get_result",
                }

                async def call(name, arguments):
                    value = await session.call_tool(name, arguments)
                    assert not value.isError, value
                    return value.structuredContent or json.loads(value.content[0].text)

                key = (await call("upload_begin", {"filename": "a.txt", "size_bytes": 5}))["upload_id"]
                await call("upload_chunk", {"upload_id": key, "index": 0, "data_base64": "aGVsbG8="})
                file_id = (await call("upload_finish", {"upload_id": key}))["file_id"]
                job = (await call("start_summary", {"file_ids": [file_id]}))["job_id"]
                assert (await call("get_status", {"job_id": job}))["status"] == "pending"
                assert await app.state.worker.run_once()
                result = await session.call_tool("get_result", {"job_id": job})
                assert "hello" in result.content[0].text
        # New connection/token cannot access another principal's durable job.
        async with await connection("bob") as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=10)
            ) as session:
                await session.initialize()
                result = await session.call_tool("get_result", {"job_id": job})
                assert result.isError
