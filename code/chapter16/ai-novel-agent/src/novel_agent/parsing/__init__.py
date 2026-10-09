"""Deterministic, side-effect-free document parsing boundaries."""

from novel_agent.parsing.contracts import (
    DocumentParser,
    ParsedDocument,
    ParserBudgets,
    ParseRequest,
    ParserIdentity,
    ParserSection,
    SectionSourceRef,
)

__all__ = [
    "DocumentParser",
    "ParsedDocument",
    "ParseRequest",
    "ParserBudgets",
    "ParserIdentity",
    "ParserSection",
    "SectionSourceRef",
]
