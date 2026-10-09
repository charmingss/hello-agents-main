"""Provider-neutral embedding contracts."""

from novel_agent.embeddings.ports import (
    EmbeddingBatch,
    EmbeddingIdentity,
    EmbeddingLimits,
    EmbeddingProvider,
    EmbeddingProviderError,
    embed_bounded,
)

__all__ = [
    "EmbeddingBatch",
    "EmbeddingIdentity",
    "EmbeddingLimits",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "embed_bounded",
]
