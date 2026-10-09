"""Collect LLM-summarized characters from source analysis records.

打通用户场景的关键链路：范文 → LLM 分析总结 → 人物特色/能力/事迹 → 生成上下文。
不依赖 promote_character（未实现），直接查询项目的分析记录，
提取 original.characters 中的人物（含阶段一新增的 abilities/deeds）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from novel_agent.db.models.analysis import SourceAnalysisRecord
from novel_agent.vector.schema import TenantProjectScope

MAX_ANALYSIS_RECORDS = 10
MAX_SOURCE_CHARACTERS = 30


def _claim_texts(claims: object) -> list[str]:
    """从 Claim 列表（dict 结构）提取 text 字段，忽略无效条目。"""
    if not isinstance(claims, list):
        return []
    texts: list[str] = []
    for claim in claims:
        if isinstance(claim, dict):
            text = claim.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


async def collect_source_characters(
    session: AsyncSession,
    scope: TenantProjectScope,
) -> list[dict[str, Any]]:
    """查询项目的范文分析记录，提取 LLM 总结出的人物列表。

    每个人物输出：name / aliases / description / traits / abilities / deeds
    （均为文本列表，Claim 元数据不注入生成上下文）。
    按 name 去重（最新分析优先），限制总条数防止 token 膨胀。
    """
    result = await session.execute(
        select(SourceAnalysisRecord.original)
        .where(
            SourceAnalysisRecord.tenant_id == scope.tenant_id,
            SourceAnalysisRecord.project_id == scope.project_id,
        )
        .order_by(
            SourceAnalysisRecord.created_at.desc(),
            SourceAnalysisRecord.id,
        )
        .limit(MAX_ANALYSIS_RECORDS)
    )
    characters: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for (original,) in result.all():
        if not isinstance(original, dict):
            continue
        raw_characters = original.get("characters", [])
        if not isinstance(raw_characters, list):
            continue
        for char in raw_characters:
            if not isinstance(char, dict):
                continue
            name = char.get("name")
            if not isinstance(name, str) or not name.strip() or name in seen_names:
                continue
            seen_names.add(name)
            aliases_raw = char.get("aliases", [])
            characters.append(
                {
                    "name": name.strip(),
                    "aliases": [a.strip() for a in aliases_raw if isinstance(a, str) and a.strip()]
                    if isinstance(aliases_raw, list)
                    else [],
                    "description": _claim_texts(char.get("description", [])),
                    "traits": _claim_texts(char.get("traits", [])),
                    "abilities": _claim_texts(char.get("abilities", [])),
                    "deeds": _claim_texts(char.get("deeds", [])),
                }
            )
            if len(characters) >= MAX_SOURCE_CHARACTERS:
                return characters
    return characters


__all__ = [
    "MAX_ANALYSIS_RECORDS",
    "MAX_SOURCE_CHARACTERS",
    "collect_source_characters",
]
