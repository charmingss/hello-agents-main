from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from novel_agent.analysis.contracts import (
    PROMPT_VERSION,
    AnalysisError,
    AnalysisProvider,
    Candidate,
    Evidence,
    SourceIdentity,
    Window,
)
from novel_agent.vector.schema import TenantProjectScope


class AnalysisAuthority(Protocol):
    async def load(
        self, scope: TenantProjectScope, source_id: UUID
    ) -> tuple[SourceIdentity, Window]: ...

    async def recheck(self, scope: TenantProjectScope, identity: SourceIdentity) -> None: ...


def validate_candidate(raw: str, window: Window) -> dict[str, Any]:
    try:
        candidate = Candidate.model_validate_json(raw)
        sections = {section.id: section for section in window.sections}

        def citation(evidence: Evidence) -> dict[str, Any]:
            section = sections[evidence.section_id]
            offset = section.text.find(evidence.quote)
            if offset < 0:
                raise ValueError("quote is outside submitted evidence")
            return {
                **evidence.model_dump(mode="json"),
                "section_index": section.index,
                "section_heading": section.heading,
                "source_ref_kind": section.source_ref_kind,
                "source_ref_index": section.source_ref_index,
                "source_ref_subindex": section.source_ref_subindex,
                "char_start": section.char_start + offset,
                "char_end": section.char_start + offset + len(evidence.quote),
            }

        result = candidate.model_dump(mode="json")
        for claim, output in zip(candidate.summary, result["summary"], strict=True):
            output["evidence"] = [citation(e) for e in claim.evidence]
        for character, output in zip(candidate.characters, result["characters"], strict=True):
            output["evidence"] = [citation(e) for e in character.evidence]
            # Identity strings must actually occur in the character's supporting quotations.
            for name in [character.name, *character.aliases]:
                if not any(name in evidence.quote for evidence in character.evidence):
                    raise ValueError("unsupported identity")
            for field in ("description", "traits"):
                for claim, item in zip(getattr(character, field), output[field], strict=True):
                    item["evidence"] = [citation(e) for e in claim.evidence]
        return result
    except (ValidationError, ValueError, KeyError, TypeError):
        raise AnalysisError("analysis_invalid_output", 502) from None


class SourceAnalysisService:
    def __init__(self, authority: AnalysisAuthority, provider: AnalysisProvider) -> None:
        self._authority = authority
        self._provider = provider

    async def preview(self, scope: TenantProjectScope, source_id: UUID) -> dict[str, Any]:
        identity, window = await self._authority.load(scope, source_id)
        try:
            raw = await self._provider.generate(window)
        except AnalysisError:
            raise
        except Exception:
            raise AnalysisError("analysis_unavailable", 502) from None
        candidate = validate_candidate(raw, window)
        await self._authority.recheck(scope, identity)
        return {
            "status": "candidate",
            "source_document_id": str(identity.source_id),
            "parse_run_id": str(identity.parse_run_id),
            "product_version": identity.product_version,
            "product_sha256": identity.product_sha256,
            "source_name": identity.display_name,
            "prompt_version": PROMPT_VERSION,
            "provider": self._provider.provider,
            "model": self._provider.model,
            "coverage": {
                "char_count": window.char_count,
                "partial": window.partial,
                "sections": [
                    {
                        "section_id": str(s.id),
                        "section_index": s.index,
                        "char_start": s.char_start,
                        "char_end": s.char_start + len(s.text),
                    }
                    for s in window.sections
                ],
            },
            **candidate,
        }
