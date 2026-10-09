"""ChapterProvider protocol + Fake/DeepSeek implementations.

Phase 5 — 单章生成 Provider 协议，与 OutlineProvider/ExtractionProvider 模式一致：
- FakeChapterProvider: 离线测试用，返回固定单章 JSON
- DeepSeekChapterProvider: 复用 DeepSeek httpx 客户端模式
"""

import asyncio
import json
from typing import Any, Protocol

import httpx

from novel_agent.analysis.chapter import ChapterResult
from novel_agent.analysis.contracts import AnalysisError
from novel_agent.config import Settings

MAX_RESPONSE_BYTES = 256_000
MAX_CONTENT_BYTES = 220_000


class ChapterProvider(Protocol):
    """单章生成 Provider 协议：接收上下文 JSON，返回 ChapterResult JSON 字符串。"""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def generate(self, context_json: dict[str, Any]) -> str: ...

    async def aclose(self) -> None: ...


def _build_messages(context_json: dict[str, Any]) -> list[dict[str, str]]:
    """构建单章生成 prompt 的消息列表。"""
    instructions = (
        "You are a novel chapter writer. Given the outline, previous chapter summaries, "
        "and story settings (characters, world entries, foreshadowing, style, events, "
        "relations), write the full text of the requested chapter. "
        "If memories are provided, they are established facts — keep character states, "
        "world details, and plot threads consistent with them. "
        "If source_characters are provided, they are character profiles distilled from "
        "imported reference works — draw on their personality, abilities, and deeds to "
        "keep the cast consistent. "
        "If the style profile is provided, strictly follow it: match the narrative "
        "voice (e.g. third-person limited), tense, pacing, tone, vocabulary level, "
        "and sentence structure described in the style fields. "
        "If retrieved_passages are provided, use them as reference material for tone, "
        "style, and narrative techniques — but do not copy them verbatim. "
        "Return only a JSON object matching this schema: "
        + json.dumps(ChapterResult.model_json_schema(), ensure_ascii=False)
        + "\nRules: "
        "The content must be a complete, continuous narrative in the source language. "
        "Follow the chapter's summary and key events exactly. "
        "Keep character names and world details consistent with the story settings. "
        "The summary field is a one-paragraph abstract of this chapter for later reference. "
        "Do not invent characters not present in the context."
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(context_json, ensure_ascii=False)},
    ]


class FakeChapterProvider:
    """离线测试用 Provider，返回固定的单章 JSON。"""

    provider = "fake"
    model = "fake-chapter"

    async def generate(self, context_json: dict[str, Any]) -> str:
        order_index = context_json.get("order_index", 1)
        current = context_json.get("current_chapter", {})
        content = (
            f"第{order_index}章正文。" + (current.get("summary", "") or "本章内容梗概。")
        ) * 30
        result = {
            "title": current.get("title") or f"第{order_index}章",
            "content": content,
            "summary": (current.get("summary") or f"第{order_index}章内容摘要")[:2000],
        }
        return json.dumps(result, ensure_ascii=False)

    async def aclose(self) -> None:
        pass


class DeepSeekChapterProvider:
    """DeepSeek 单章生成 Provider，复用 httpx 客户端模式。"""

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
        chapter_model = getattr(settings, "chapter_model", "")
        self.model = chapter_model or settings.analysis_model

    @property
    def _api_key(self) -> str | None:
        chapter_key = getattr(self._settings, "chapter_api_key", None)
        if chapter_key is not None:
            val: str = chapter_key.get_secret_value()
            if val.strip():
                return val
        if self._settings.analysis_api_key is not None:
            val = self._settings.analysis_api_key.get_secret_value()
            if val.strip():
                return val
        return None

    @property
    def _base_url(self) -> str:
        chapter_url = getattr(self._settings, "chapter_base_url", "")
        if chapter_url:
            return chapter_url.rstrip("/")
        return self._settings.analysis_base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, context_json: dict[str, Any]) -> str:
        key = self._api_key
        if not self.model or key is None:
            raise AnalysisError("chapter_not_configured", 503)
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
                    raise AnalysisError("chapter_unavailable", 502)
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AnalysisError("chapter_invalid_output", 502)
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
            raise AnalysisError("chapter_unavailable", 502) from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise AnalysisError("chapter_invalid_output", 502) from None
