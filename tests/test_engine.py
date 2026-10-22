import httpx
import pytest

from api_summary.engine import Summarizer
from api_summary.store import ServiceError


async def test_hierarchical_summary_covers_all_sources(settings):
    settings.chunk_tokens = 256
    engine = Summarizer(settings)
    await engine.client.aclose()
    requests = []

    def respond(request):
        import json

        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Résumé synthétique."}}]}
        )

    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await engine.summarize(
            [("premier.txt", "préfecture " * 300), ("dernier.txt", "DERNIERE_INFORMATION " * 300)],
            "En français",
        )
    finally:
        await engine.close()
    sources = [r["messages"][1]["content"] for r in requests]
    assert any("premier.txt" in text for text in sources)
    assert any("dernier.txt" in text and "DERNIERE" in text for text in sources)
    assert len(requests) > 2
    assert "Merge all supplied partial summaries" in requests[-1]["messages"][0]["content"]
    assert result == "Résumé synthétique."


async def test_truncated_model_response_is_a_failure(settings):
    engine = Summarizer(settings)
    await engine.client.aclose()
    engine.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"finish_reason": "length", "message": {"content": "incomplete"}}]}
            )
        )
    )
    try:
        with pytest.raises(ServiceError, match="token limit"):
            await engine.summarize([("a.txt", "hello")], "Résume")
    finally:
        await engine.close()


async def test_failed_passage_cancels_sibling_requests(settings):
    import asyncio

    engine = Summarizer(settings)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def invoke(source, instructions, combine=False):
        if source == "bad":
            await started.wait()
            raise ServiceError("failed passage")
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    engine.invoke = invoke
    try:
        with pytest.raises(ServiceError):
            await engine.batch(["bad", "slow"], "Résume")
        assert cancelled.is_set()
    finally:
        await engine.close()
