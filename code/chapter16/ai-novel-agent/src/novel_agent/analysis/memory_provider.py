"""MemoryProvider protocol + Fake/DeepSeek implementations.

Phase 6 — 写作记忆提取 Provider，与 ChapterProvider 模式一致。
"""

import asyncio
import json
from typing import Any, Protocol

import httpx

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.memory import MemoryResult
from novel_agent.config import Settings

MAX_RESPONSE_BYTES = 256_000
MAX_CONTENT_BYTES = 220_000


class MemoryProvider(Protocol):
    """记忆提取 Provider 协议：接收章节上下文，返回 MemoryResult JSON。"""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def generate(self, context_json: dict[str, Any]) -> str: ...

    async def aclose(self) -> None: ...


def _build_messages(context_json: dict[str, Any]) -> list[dict[str, str]]:
    """构建记忆提取 prompt。"""
    instructions = (
        "You are a story fact extractor. Read the given text and extract key "
        "facts worth remembering: character developments, world details, plot "
        "events, foreshadowing, and relationships. "
        'The text may be a generated chapter (key "chapter") or a reference '
        'document (key "source_text"). '
        "Return only a JSON object matching this schema: "
        + json.dumps(MemoryResult.model_json_schema(), ensure_ascii=False)
        + "\nRules: "
        "Each item is one concise factual statement in the source language. "
        "Use category 'character' for character traits/state, 'world' for world "
        "entries, 'plot' for events, 'foreshadowing' for planted/unresolved "
        "threads, 'relation' for relationships. "
        "Do not invent facts not present in the text. "
        'If there are no noteworthy facts, return {"items": []}.'
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(context_json, ensure_ascii=False)},
    ]


class FakeMemoryProvider:
    """离线测试 Provider，返回固定的记忆条目 JSON。"""

    provider = "fake"
    model = "fake-memory"

    async def generate(self, context_json: dict[str, Any]) -> str:
        chapter = context_json.get("chapter", {})
        source_text = context_json.get("source_text", "")
        title = chapter.get("title") or "范文" if not source_text else "范文"
        result = {
            "items": [
                {"category": "plot", "content": f"{title}中发生了关键情节。"},
                {"category": "character", "content": f"{title}中人物状态发生变化。"},
            ]
        }
        return json.dumps(result, ensure_ascii=False)

    async def aclose(self) -> None:
        pass


class DeepSeekMemoryProvider:
    """DeepSeek 记忆提取 Provider，复用 httpx 客户端模式。"""

    provider = "deepseek"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        memory_model = getattr(settings, "memory_model", "")
        self.model = memory_model or settings.analysis_model

    @property
    def _api_key(self) -> str | None:
        memory_key = getattr(self._settings, "memory_api_key", None)
        if memory_key is not None:
            val: str = memory_key.get_secret_value()
            if val.strip():
                return val
        if self._settings.analysis_api_key is not None:
            val = self._settings.analysis_api_key.get_secret_value()
            if val.strip():
                return val
        return None

    @property
    def _base_url(self) -> str:
        memory_url = getattr(self._settings, "memory_base_url", "")
        if memory_url:
            return memory_url.rstrip("/")
        return self._settings.analysis_base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, context_json: dict[str, Any]) -> str:
        key = self._api_key
        if not self.model or key is None:
            raise AnalysisError("memory_not_configured", 503)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.analysis_timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            )
        try:
            async with (
                asyncio.timeout(self._settings.analysis_timeout_seconds),
                self._client.stream(
                    "POST",
                    self._base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": self.model,
                        "messages": _build_messages(context_json),
                        "response_format": {"type": "json_object"},
                        "max_tokens": 8192,
                        "thinking": {"type": "disabled"},
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise AnalysisError("memory_unavailable", 502)
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AnalysisError("memory_invalid_output", 502)
                    data.extend(chunk)
            envelope = json.loads(data)
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("invalid choices")
            choice = choices[0]
            content = choice["message"]["content"]
            if (
                choice["finish_reason"] != "stop"
                or not isinstance(content, str)
                or not content.strip()
                or len(content.encode("utf-8")) > MAX_CONTENT_BYTES
            ):
                raise ValueError("invalid content")
            return content
        except AnalysisError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise AnalysisError("memory_unavailable", 502) from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise AnalysisError("memory_invalid_output", 502) from None
