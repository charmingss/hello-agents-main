"""OutlineProvider protocol + Fake/DeepSeek implementations.

Phase 4 — 大纲生成 Provider 协议，与 ExtractionProvider 模式一致：
- FakeOutlineProvider: 离线测试用，返回固定 JSON
- DeepSeekOutlineProvider: 复用 DeepSeek httpx 客户端模式
"""

import asyncio
import json
from typing import Any, Protocol

import httpx

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.outline import OutlineResult
from novel_agent.config import Settings

MAX_RESPONSE_BYTES = 256_000
MAX_CONTENT_BYTES = 100_000


class OutlineProvider(Protocol):
    """大纲生成 Provider 协议：接收上下文 JSON，返回 OutlineResult JSON 字符串。"""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def generate(self, context_json: dict[str, Any]) -> str: ...

    async def aclose(self) -> None: ...


def _build_messages(context_json: dict[str, Any]) -> list[dict[str, str]]:
    """构建大纲生成 prompt 的消息列表。"""
    instructions = (
        "You are a novel outline generator. Given the story bible context (characters, "
        "world entries, foreshadowing, style, events, relations), generate a structured "
        "novel outline with chapters. "
        "If user_prompt is provided, it is the author's creative direction — build the "
        "story premise, chapter arc, and key events around it while staying consistent "
        "with the existing context. "
        "If source_characters are provided, they are characters distilled from imported "
        "reference works (personality, abilities, deeds) — reuse them as story material "
        "when they fit the creative direction. "
        "If memories are provided, they are established facts from earlier chapters or "
        "reference materials — keep the outline consistent with them. "
        "If the style profile is provided, reflect it in the outline: match the "
        "narrative voice, pacing, and tone when structuring chapters and describing "
        "events. If retrieved_passages are provided, use them as reference material "
        "for narrative structure and pacing — but create original content. "
        "Return only a JSON object matching this schema: "
        + json.dumps(OutlineResult.model_json_schema(), ensure_ascii=False)
        + "\nRules: "
        "Each chapter needs a title, summary, and key_events list. "
        "The outline should follow the narrative arc naturally. "
        "All text uses the original language of the source material. "
        "Do not invent characters not present in the context. "
        'Empty example: {"title":"", "premise":"", "chapters":[]}'
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(context_json, ensure_ascii=False)},
    ]


class FakeOutlineProvider:
    """离线测试用 Provider，返回固定的大纲 JSON。"""

    provider = "fake"
    model = "fake-outline"

    async def generate(self, context_json: dict[str, Any]) -> str:
        target = context_json.get("target_chapters", 3)
        chapters = []
        for i in range(1, min(target, 3) + 1):
            chapters.append(
                {
                    "order_index": i,
                    "title": f"第{i}章",
                    "summary": f"第{i}章的内容梗概",
                    "key_events": [f"事件{i}-1", f"事件{i}-2"],
                    "notes": f"第{i}章的备注",
                }
            )
        result = {
            "title": "测试小说",
            "premise": "一个关于勇气与成长的故事",
            "chapters": chapters,
        }
        return json.dumps(result, ensure_ascii=False)

    async def aclose(self) -> None:
        pass


class DeepSeekOutlineProvider:
    """DeepSeek 大纲生成 Provider，复用 httpx 客户端模式。"""

    provider = "deepseek"

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        outline_model = getattr(settings, "outline_model", "")
        self.model = outline_model or settings.analysis_model

    @property
    def _api_key(self) -> str | None:
        outline_key = getattr(self._settings, "outline_api_key", None)
        if outline_key is not None:
            val: str = outline_key.get_secret_value()
            if val.strip():
                return val
        if self._settings.analysis_api_key is not None:
            val = self._settings.analysis_api_key.get_secret_value()
            if val.strip():
                return val
        return None

    @property
    def _base_url(self) -> str:
        outline_url = getattr(self._settings, "outline_base_url", "")
        if outline_url:
            return outline_url.rstrip("/")
        return self._settings.analysis_base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, context_json: dict[str, Any]) -> str:
        key = self._api_key
        if not self.model or key is None:
            raise AnalysisError("outline_not_configured", 503)
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
                    raise AnalysisError("outline_unavailable", 502)
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AnalysisError("outline_invalid_output", 502)
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
            raise AnalysisError("outline_unavailable", 502) from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise AnalysisError("outline_invalid_output", 502) from None
