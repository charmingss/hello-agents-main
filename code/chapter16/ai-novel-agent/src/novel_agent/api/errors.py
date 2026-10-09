import logging
from enum import StrEnum
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from novel_agent.sources.api_service import (
    CorruptSource,
    InvalidSourceCursor,
    SourceNotFound,
    StorageUnavailable,
    UploadIntentExpired,
    UploadValidationInProgress,
)
from novel_agent.sources.errors import (
    AuthorizationProvenanceError,
    SourceConflictError,
    SourceHashMismatchError,
    SourceSizeLimitError,
    UnsupportedSourceTypeError,
    UploadQuotaExceededError,
)

logger = logging.getLogger(__name__)


class InvalidTokenReason(StrEnum):
    ALGORITHM = "algorithm"
    AUDIENCE = "audience"
    CLAIMS = "claims"
    EXPIRED = "expired"
    ISSUER = "issuer"
    KEY = "key"
    METADATA = "metadata"
    NETWORK = "network"
    SIGNATURE = "signature"


class InvalidTokenError(Exception):
    """Internal auth failure without retaining bearer credentials."""

    public_code = "invalid_token"

    def __init__(self, reason: InvalidTokenReason, cause: BaseException | None = None) -> None:
        self.reason = reason
        self.cause_type = type(cause).__name__ if cause is not None else None
        super().__init__(reason.value)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(reason={self.reason.value!r}, cause_type={self.cause_type!r})"
        )

    def as_http_exception(self) -> HTTPException:
        return invalid_token_http_exception()


def invalid_token_http_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": InvalidTokenError.public_code},
        headers={"WWW-Authenticate": "Bearer"},
    )


class ProjectNotFoundError(Exception):
    pass


async def workflow_not_found_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _problem(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        problem_type="workflow-not-found",
        title="Workflow not found",
        detail="The requested workflow does not exist.",
    )


async def workflow_service_unavailable_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    del exc
    return _problem(
        request,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        problem_type="workflow-service-unavailable",
        title="Workflow service unavailable",
        detail="The workflow service is temporarily unavailable.",
    )


def _problem(
    request: Request,
    *,
    status_code: int,
    problem_type: str,
    title: str,
    detail: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    trace_id = trace_id or uuid4().hex
    body: dict[str, Any] = {
        "type": f"https://novel-agent.dev/problems/{problem_type}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
        "trace_id": trace_id,
    }
    if extra:
        body.update(extra)
    return JSONResponse(
        body,
        status_code=status_code,
        media_type="application/problem+json",
        headers={"X-Trace-ID": trace_id},
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        return await unexpected_exception_handler(request, exc)
    if "/sources/uploads" in request.url.path:
        raw_errors = exc.errors()
        locations = {str(part) for error in raw_errors for part in error["loc"]}
        messages = " ".join(str(error["msg"]).casefold() for error in raw_errors)
        if "unsupported source extension" in messages or "media_type" in locations:
            return _problem(
                request,
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                problem_type="unsupported-source-type",
                title="Unsupported source type",
                detail="The source type is not supported.",
            )
        if "authorization" in locations:
            return _problem(
                request,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                problem_type="authorization-invalid",
                title="Authorization invalid",
                detail="The source authorization declaration is invalid.",
            )
        if "byte_size" in locations:
            return _problem(
                request,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                problem_type="source-size-limit",
                title="Source too large",
                detail="The source exceeds the configured upload limit.",
            )
        if "content_sha256" in locations:
            return _problem(
                request,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                problem_type="source-hash-mismatch",
                title="Source hash invalid",
                detail="The declared source hash is malformed.",
            )
    errors = [
        {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return _problem(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        problem_type="validation-error",
        title="Request validation failed",
        detail="One or more request fields are invalid.",
        extra={"errors": errors},
    )


async def project_not_found_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _problem(
        request,
        status_code=status.HTTP_404_NOT_FOUND,
        problem_type="project-not-found",
        title="Project not found",
        detail="The requested project does not exist.",
    )


async def project_conflict_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _problem(
        request,
        status_code=status.HTTP_409_CONFLICT,
        problem_type="project-conflict",
        title="Project conflict",
        detail="The project conflicts with an existing resource.",
    )


async def source_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    definitions: dict[type[Exception], tuple[int, str, str, str]] = {
        SourceNotFound: (
            404,
            "source-not-found",
            "Source not found",
            "The requested resource does not exist.",
        ),
        UploadIntentExpired: (
            410,
            "upload-intent-expired",
            "Upload intent expired",
            "The upload intent has expired.",
        ),
        StorageUnavailable: (
            503,
            "object-storage-unavailable",
            "Object storage unavailable",
            "Object storage is temporarily unavailable.",
        ),
        CorruptSource: (
            422,
            "corrupt-source",
            "Corrupt source",
            "The uploaded content failed integrity or format validation.",
        ),
        SourceConflictError: (
            409,
            "source-conflict",
            "Source conflict",
            "The request conflicts with the existing upload or source.",
        ),
        SourceHashMismatchError: (
            422,
            "source-hash-mismatch",
            "Source hash mismatch",
            "The uploaded content hash does not match.",
        ),
        SourceSizeLimitError: (
            413,
            "source-size-limit",
            "Source too large",
            "The source exceeds the configured upload limit.",
        ),
        UnsupportedSourceTypeError: (
            415,
            "unsupported-source-type",
            "Unsupported source type",
            "The source type is not supported.",
        ),
        AuthorizationProvenanceError: (
            422,
            "authorization-invalid",
            "Authorization invalid",
            "The source authorization declaration is invalid.",
        ),
        UploadQuotaExceededError: (
            429,
            "upload-quota-exceeded",
            "Upload quota exceeded",
            "The project has too many active upload sessions.",
        ),
        InvalidSourceCursor: (
            400,
            "invalid-source-cursor",
            "Invalid source cursor",
            "The source pagination cursor is invalid.",
        ),
        UploadValidationInProgress: (
            409,
            "upload-validation-in-progress",
            "Upload validation in progress",
            "This upload is currently being validated; retry shortly.",
        ),
    }
    status_code, problem_type, title, detail = definitions[type(exc)]
    response = _problem(
        request,
        status_code=status_code,
        problem_type=problem_type,
        title=title,
        detail=detail,
    )
    if isinstance(exc, UploadValidationInProgress):
        response.headers["Retry-After"] = "3"
    return response


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = uuid4().hex
    exception_type = type(exc).__name__
    safe_exception = RuntimeError("exception details redacted")
    logger.exception(
        "Unhandled request failure trace_id=%s exception_type=%s",
        trace_id,
        exception_type,
        extra={"trace_id": trace_id},
        exc_info=(type(safe_exception), safe_exception, exc.__traceback__),
    )
    return _problem(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        problem_type="internal-error",
        title="Internal server error",
        detail="An unexpected error occurred.",
        trace_id=trace_id,
    )
