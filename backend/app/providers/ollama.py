"""Ollama provider using only documented REST endpoints (https://github.com/ollama/ollama/blob/main/docs/api.md)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.providers.base import (
    ChatMessage,
    GenerationOptions,
    HealthStatus,
    ModelInfo,
    ModelNotFound,
    ProviderError,
    ProviderUnavailable,
    StreamChunk,
    ToolCall,
    ToolsUnsupported,
)


def _options(opts: GenerationOptions) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if opts.temperature is not None:
        out["temperature"] = opts.temperature
    if opts.top_p is not None:
        out["top_p"] = opts.top_p
    if opts.num_ctx is not None:
        out["num_ctx"] = opts.num_ctx
    if opts.max_output_tokens is not None:
        out["num_predict"] = opts.max_output_tokens
    return out


def _error_text(resp: httpx.Response, body: bytes) -> str:
    try:
        return str(json.loads(body).get("error") or body.decode(errors="replace"))
    except Exception:
        return body.decode(errors="replace")[:500] or f"HTTP {resp.status_code}"


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, timeout_s: float = 600.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s, connect=3.0)
        self._transport = transport

    def _client(self, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
        # trust_env=False: never route local inference through a system proxy.
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout or self._timeout, transport=self._transport, trust_env=False
        )

    async def _get_json(self, path: str, timeout: float = 5.0) -> Any:
        try:
            async with self._client(httpx.Timeout(timeout, connect=2.0)) as c:
                r = await c.get(path)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            raise ProviderUnavailable(f"Cannot reach Ollama at {self.base_url}: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise ProviderError(_error_text(r, r.content))
        return r.json()

    async def health(self) -> HealthStatus:
        try:
            data = await self._get_json("/api/version", timeout=3.0)
            return HealthStatus(available=True, version=data.get("version"), url=self.base_url)
        except ProviderError as e:
            return HealthStatus(available=False, error=str(e), url=self.base_url)

    async def list_models(self) -> list[ModelInfo]:
        data = await self._get_json("/api/tags")
        models = []
        for m in data.get("models", []):
            details = m.get("details") or {}
            models.append(
                ModelInfo(
                    name=m.get("name") or m.get("model", "?"),
                    size_bytes=m.get("size"),
                    modified_at=m.get("modified_at"),
                    family=details.get("family"),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                )
            )
        return models

    async def loaded_models(self) -> list[dict[str, Any]]:
        data = await self._get_json("/api/ps")
        return [
            {
                "name": m.get("name"),
                "size_bytes": m.get("size"),
                "size_vram_bytes": m.get("size_vram"),
                "expires_at": m.get("expires_at"),
            }
            for m in data.get("models", [])
        ]

    def _chat_body(self, model: str, messages: list[ChatMessage], opts: GenerationOptions, stream: bool) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": [_msg(m) for m in messages],
            "stream": stream,
        }
        o = _options(opts)
        if o:
            body["options"] = o
        if opts.keep_alive:
            body["keep_alive"] = opts.keep_alive
        if opts.response_format is not None:
            body["format"] = opts.response_format
        if opts.tools:
            body["tools"] = opts.tools
        return body

    @staticmethod
    def _raise_for(status: int, msg: str) -> None:
        if "does not support tools" in msg.lower():
            raise ToolsUnsupported(msg)
        if status == 404 or "not found" in msg.lower():
            raise ModelNotFound(msg)
        raise ProviderError(msg)

    async def chat_stream(
        self, model: str, messages: list[ChatMessage], options: GenerationOptions
    ) -> AsyncIterator[StreamChunk]:
        body = self._chat_body(model, messages, options, stream=True)
        try:
            async with self._client() as c, c.stream("POST", "/api/chat", json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    self._raise_for(r.status_code, _error_text(r, raw))
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        self._raise_for(500, str(data["error"]))
                    message = data.get("message") or {}
                    text = message.get("content", "")
                    calls = _tool_calls(message)
                    if data.get("done"):
                        yield StreamChunk(text=text, done=True, stats=_stats(data), tool_calls=calls)
                        return
                    if text or calls:
                        yield StreamChunk(text=text, tool_calls=calls)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderUnavailable(f"Cannot reach Ollama at {self.base_url}") from e
        except httpx.RemoteProtocolError as e:
            raise ProviderUnavailable("Ollama closed the connection (was it restarted?)") from e

    async def chat(self, model: str, messages: list[ChatMessage], options: GenerationOptions) -> str:
        parts: list[str] = []
        async for chunk in self.chat_stream(model, messages, options):
            parts.append(chunk.text)
        return "".join(parts)

    async def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        try:
            async with (
                self._client(httpx.Timeout(None, connect=3.0)) as c,
                c.stream("POST", "/api/pull", json={"model": model, "stream": True}) as r,
            ):
                if r.status_code != 200:
                    raw = await r.aread()
                    raise ProviderError(_error_text(r, raw))
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise ProviderError(str(data["error"]))
                    yield data
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderUnavailable(f"Cannot reach Ollama at {self.base_url}") from e

    async def embed(self, model: str, texts: list[str], keep_alive: str | None = None) -> list[list[float]]:
        """POST /api/embed. Used only by the optional semantic index."""
        body: dict[str, Any] = {"model": model, "input": texts}
        if keep_alive:
            body["keep_alive"] = keep_alive
        try:
            async with self._client() as c:
                r = await c.post("/api/embed", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderUnavailable(f"Cannot reach Ollama at {self.base_url}") from e
        if r.status_code != 200:
            self._raise_for(r.status_code, _error_text(r, r.content))
        vecs = r.json().get("embeddings") or []
        if len(vecs) != len(texts):
            raise ProviderError("Ollama returned an unexpected number of embeddings")
        return vecs

    async def unload(self, model: str) -> None:
        async with self._client(httpx.Timeout(30, connect=3.0)) as c:
            r = await c.post("/api/generate", json={"model": model, "keep_alive": 0})
            if r.status_code != 200:
                raise ProviderError(_error_text(r, r.content))


def _msg(m: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
    if m.tool_name:
        out["tool_name"] = m.tool_name
    return out


def _tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # some models return a JSON string
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        if fn.get("name"):
            calls.append(ToolCall(name=str(fn["name"]), arguments=args if isinstance(args, dict) else {}))
    return calls


def _stats(data: dict[str, Any]) -> dict[str, Any]:
    ns = 1e9
    out: dict[str, Any] = {
        "prompt_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
        "done_reason": data.get("done_reason"),
    }
    if data.get("total_duration"):
        out["total_s"] = data["total_duration"] / ns
    if data.get("load_duration"):
        out["load_s"] = data["load_duration"] / ns
    if data.get("eval_count") and data.get("eval_duration"):
        out["tokens_per_s"] = data["eval_count"] / (data["eval_duration"] / ns)
    return out
