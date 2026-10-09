"""RAG retriever protocol and implementations for generation-time passage retrieval.

Phase 3 — RAG 增强生成：在章节/大纲生成时检索向量库中的范文片段，注入生成上下文。
- RagRetriever 协议：输入 scope + query，返回范文片段列表
- SourceSearchRagRetriever：基于 SourceSearchService 的生产实现（需要 Qdrant + embedding）
- FakeRagRetriever：离线测试用，返回固定片段
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.vector.schema import TenantProjectScope

if TYPE_CHECKING:
    from novel_agent.api.routes.search import SourceSearchService

DEFAULT_RAG_TOP_K = 5
MAX_RAG_TOP_K = 20


class RagRetriever(Protocol):
    """检索范文片段的协议。"""

    async def retrieve(
        self,
        scope: TenantProjectScope,
        query: str,
        top_k: int = DEFAULT_RAG_TOP_K,
    ) -> list[dict[str, Any]]:
        """返回检索到的范文片段列表。

        每个片段的格式：
        {
            "text": str,              # 范文片段正文
            "citation_label": str,    # 引用标签（如 "第3章"）
            "citation_heading": str | None,  # 段落标题
            "score": float,           # 相关度得分
        }
        """
        ...


class SourceSearchRagRetriever:
    """基于 SourceSearchService 的 RAG 检索器（生产环境）。

    依赖 Qdrant 向量库 + embedding provider。
    检索失败时返回空列表（降级而非报错），保证生成流程不中断。
    """

    def __init__(self, search_service: SourceSearchService) -> None:
        self._search = search_service

    async def retrieve(
        self,
        scope: TenantProjectScope,
        query: str,
        top_k: int = DEFAULT_RAG_TOP_K,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        if top_k < 1:
            top_k = 1
        if top_k > MAX_RAG_TOP_K:
            top_k = MAX_RAG_TOP_K
        try:
            response = await self._search.search(
                scope=scope,
                query=query,
                limit=top_k,
                where=None,
            )
        except AnalysisError:
            # 检索失败降级为空结果，不中断生成
            return []
        passages: list[dict[str, Any]] = []
        for hit in response.hits:
            citation = hit.citation
            passages.append(
                {
                    "text": citation.section_text
                    if hasattr(citation, "section_text")
                    else str(hit.text),
                    "citation_label": getattr(citation, "label", "") or "",
                    "citation_heading": getattr(citation, "heading", None),
                    "score": hit.score,
                }
            )
        return passages


class FakeRagRetriever:
    """离线测试用 RAG 检索器，返回固定范文片段。"""

    def __init__(self, passages: list[dict[str, Any]] | None = None) -> None:
        self._passages = passages or [
            {
                "text": "范文片段：主角在雨夜的巷子里遭遇伏击，凭借敏捷的身手脱困。",
                "citation_label": "范文第1章",
                "citation_heading": "雨夜",
                "score": 0.95,
            }
        ]
        self.calls: list[tuple[TenantProjectScope, str, int]] = []

    async def retrieve(
        self,
        scope: TenantProjectScope,
        query: str,
        top_k: int = DEFAULT_RAG_TOP_K,
    ) -> list[dict[str, Any]]:
        self.calls.append((scope, query, top_k))
        return self._passages[:top_k]


__all__ = [
    "DEFAULT_RAG_TOP_K",
    "MAX_RAG_TOP_K",
    "FakeRagRetriever",
    "RagRetriever",
    "SourceSearchRagRetriever",
]
