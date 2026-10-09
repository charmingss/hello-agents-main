class SourceIngestionError(Exception):
    """Base class for stable, user-safe ingestion failures."""

    code = "source_ingestion_failed"


class UnsupportedSourceTypeError(SourceIngestionError):
    code = "unsupported_source_type"


class UnsafeSourceNameError(SourceIngestionError):
    code = "unsafe_source_name"


class SourceSizeLimitError(SourceIngestionError):
    code = "source_size_limit_exceeded"


class SourceHashMismatchError(SourceIngestionError):
    code = "source_hash_mismatch"


class AuthorizationProvenanceError(SourceIngestionError):
    code = "authorization_provenance_invalid"


class SourceConflictError(SourceIngestionError):
    code = "source_conflict"


class UploadQuotaExceededError(SourceIngestionError):
    code = "upload_quota_exceeded"


class UploadIntentExpiredError(SourceIngestionError):
    code = "upload_intent_expired"


class UploadValidationInProgressError(SourceIngestionError):
    code = "upload_validation_in_progress"


class SourceStateTransitionError(SourceIngestionError):
    code = "source_state_transition_invalid"

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"invalid source state transition: {current} -> {target}")
        self.current = current
        self.target = target


__all__ = [
    "AuthorizationProvenanceError",
    "SourceHashMismatchError",
    "SourceIngestionError",
    "SourceConflictError",
    "SourceSizeLimitError",
    "SourceStateTransitionError",
    "UnsafeSourceNameError",
    "UnsupportedSourceTypeError",
    "UploadQuotaExceededError",
    "UploadIntentExpiredError",
    "UploadValidationInProgressError",
]
