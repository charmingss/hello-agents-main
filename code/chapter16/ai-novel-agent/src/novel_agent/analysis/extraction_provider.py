"""ExtractionProvider protocol + Fake/DeepSeek implementations.

Phase 3 — 提取 Provider 协议，与 AnalysisProvider 模式一致：
- FakeExtractionProvider: 离线测试用，返回固定 JSON
- DeepSeekExtractionProvider: 复用 DeepSeek httpx 客户端模式
"""

import asyncio
import json
from typing import Any, Protocol

import httpx

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.extraction import ExtractionResult
from novel_agent.config import Settings

MAX_RESPONSE_BYTES = 256_000
MAX_CONTENT_BYTES = 100_000


class ExtractionProvider(Protocol):
    """提取 Provider 协议：接收分析结果 + 原文段落，返回 ExtractionResult JSON 字符串。"""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def generate(
        self,
        analysis_json: dict[str, Any],
        sections: list[dict[str, Any]],
    ) -> str: ...

    async def aclose(self) -> None: ...


def _build_messages(
    analysis_json: dict[str, Any], sections: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """构建提取 prompt 的消息列表。"""
    instructions = (
        "You are a literary analysis assistant. Given analysis results and source "
        "sections, extract events, character relations, and narrative style. "
        "Return only a JSON object matching this schema: "
        + json.dumps(ExtractionResult.model_json_schema(), ensure_ascii=False)
        + "\nRules: "
        "Events need at least one participant. Relations connect two named characters. "
        "Style is optional. Each event may include event_type (e.g. achievement, battle, "
        "relationship, growth, discovery) and outcome (the result or consequence). "
        "Character deeds from the analysis should be reflected as events with event_type "
        '"achievement". All fields use the original language of the source text. '
        "Do not invent characters not present in the analysis. "
        'Empty example: {"events": [], "relations": []}'
    )
    material = {
        "analysis": analysis_json,
        "sections": sections,
    }
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(material, ensure_ascii=False)},
    ]


class FakeExtractionProvider:
    """离线测试用 Provider，返回固定的提取结果。"""

    provider = "fake"
    model = "fake-extraction"

    async def generate(
        self,
        analysis_json: dict[str, Any],
        sections: list[dict[str, Any]],
    ) -> str:
        result = {
            "events": [
                {
                    "title": "初遇",
                    "description": "两人在雨夜相遇",
                    "participants": [
                        {"character_name": "林舟", "role": "主角"},
                        {"character_name": "苏晚"},
                    ],
                    "event_type": "encounter",
                    "outcome": "两人结盟",
                },
            ],
            "relations": [
                {
                    "relation_type": "盟友",
                    "from_character_name": "林舟",
                    "to_character_name": "苏晚",
                    "description": "雨夜结盟",
                },
            ],
            "style": {
                "narrative_voice": "第三人称限知",
                "tense": "过去时",
                "tone": "冷峻",
                "pacing": "中等",
                "vocabulary_level": "文学化",
                "sentence_structure": "长短交错",
                "notes": "注重环境描写",
            },
        }
        return json.dumps(result, ensure_ascii=False)

    async def aclose(self) -> None:
        pass


class DeepSeekExtractionProvider:
    """DeepSeek 提取 Provider，复用分析 Provider 的 httpx 客户端模式。"""

    provider = "deepseek"

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # 优先使用 extraction 专用配置，fallback 到 analysis 配置
        extraction_model = getattr(settings, "extraction_model", "")
        self.model = extraction_model or settings.analysis_model

    @property
    def _api_key(self) -> str | None:
        extraction_key = getattr(self._settings, "extraction_api_key", None)
        if extraction_key is not None:
            val: str = extraction_key.get_secret_value()
            if val.strip():
                return val
        if self._settings.analysis_api_key is not None:
            val = self._settings.analysis_api_key.get_secret_value()
            if val.strip():
                return val
        return None

    @property
    def _base_url(self) -> str:
        extraction_url = getattr(self._settings, "extraction_base_url", "")
        if extraction_url:
            return extraction_url.rstrip("/")
        return self._settings.analysis_base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(
        self,
        analysis_json: dict[str, Any],
        sections: list[dict[str, Any]],
    ) -> str:
        key = self._api_key
        if not self.model or key is None:
            raise AnalysisError("analysis_not_configured", 503)
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
                        "messages": _build_messages(analysis_json, sections),
                        "response_format": {"type": "json_object"},
                        "max_tokens": 4096,
                        "thinking": {"type": "disabled"},
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise AnalysisError("extraction_unavailable", 502)
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AnalysisError("extraction_invalid_output", 502)
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
            raise AnalysisError("extraction_unavailable", 502) from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise AnalysisError("extraction_invalid_output", 502) from None
