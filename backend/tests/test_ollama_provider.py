"""Tests against mocked Ollama HTTP responses (documented API shapes)."""

import json

import httpx
import pytest
import respx

from app.providers.base import GenerationOptions, ModelNotFound, ProviderError, ProviderUnavailable
from app.providers.ollama import OllamaProvider
from app.services.llm import GenerationStopped, LLMService

URL = "http://localhost:11434"


def ndjson(*objs) -> bytes:
    return b"".join(json.dumps(o).encode() + b"\n" for o in objs)


@respx.mock
async def test_health_and_models() -> None:
    respx.get(f"{URL}/api/version").respond(json={"version": "0.12.0"})
    respx.get(f"{URL}/api/tags").respond(json={"models": [{
        "name": "qwen2.5-coder:7b", "size": 4683087332, "modified_at": "2026-01-01T00:00:00Z",
        "details": {"family": "qwen2", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"}}]})
    p = OllamaProvider(URL)
    h = await p.health()
    assert h.available and h.version == "0.12.0"
    models = await p.list_models()
    assert models[0].name == "qwen2.5-coder:7b" and models[0].quantization == "Q4_K_M"


@respx.mock
async def test_unavailable() -> None:
    respx.get(f"{URL}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    respx.post(f"{URL}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    p = OllamaProvider(URL)
    h = await p.health()
    assert not h.available and "Cannot reach Ollama" in (h.error or "")
    with pytest.raises(ProviderUnavailable):
        await p.chat("m", [], GenerationOptions())


@respx.mock
async def test_chat_stream_and_options() -> None:
    route = respx.post(f"{URL}/api/chat").respond(content=ndjson(
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 2,
         "eval_duration": 1_000_000_000, "prompt_eval_count": 5, "total_duration": 2_000_000_000},
    ))
    p = OllamaProvider(URL)
    from app.providers.base import ChatMessage

    chunks = [c async for c in p.chat_stream("qwen2.5-coder:7b", [ChatMessage("user", "hi")],
                                             GenerationOptions(temperature=0.2, num_ctx=4096, max_output_tokens=100,
                                                               keep_alive="5m"))]
    assert "".join(c.text for c in chunks) == "Hello"
    assert chunks[-1].done and chunks[-1].stats["tokens_per_s"] == 2.0
    body = json.loads(route.calls[0].request.content)
    assert body["options"] == {"temperature": 0.2, "num_ctx": 4096, "num_predict": 100}
    assert body["keep_alive"] == "5m" and body["stream"] is True


@respx.mock
async def test_model_not_found_and_stream_error() -> None:
    respx.post(f"{URL}/api/chat").mock(side_effect=[
        httpx.Response(404, json={"error": 'model "nope" not found, try pulling it first'}),
        httpx.Response(200, content=ndjson({"error": "out of memory"})),
    ])
    p = OllamaProvider(URL)
    with pytest.raises(ModelNotFound):
        await p.chat("nope", [], GenerationOptions())
    with pytest.raises(ProviderError, match="out of memory"):
        await p.chat("m", [], GenerationOptions())


@respx.mock
async def test_pull_progress() -> None:
    respx.post(f"{URL}/api/pull").respond(content=ndjson(
        {"status": "pulling manifest"}, {"status": "downloading", "total": 10, "completed": 5}, {"status": "success"}))
    p = OllamaProvider(URL)
    events = [e async for e in p.pull("qwen2.5-coder:7b")]
    assert events[-1]["status"] == "success" and events[1]["completed"] == 5


@respx.mock
async def test_llm_service_stop_and_concurrency() -> None:
    respx.post(f"{URL}/api/chat").respond(content=ndjson(
        *[{"message": {"content": f"t{i} "}, "done": False} for i in range(50)], {"done": True}))
    svc = LLMService(OllamaProvider(URL), max_concurrent=1)
    gen = svc.new_generation("m", "chat")
    got = []
    with pytest.raises(GenerationStopped):
        async for c in svc.stream(gen, [], GenerationOptions()):
            got.append(c.text)
            if len(got) == 3:
                svc.stop(gen.id)
    assert len(got) == 3 and gen.id not in svc.active
