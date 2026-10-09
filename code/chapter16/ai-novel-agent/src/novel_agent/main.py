from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from novel_agent.analysis.authority import SqlAlchemyAnalysisAuthority
from novel_agent.analysis.chapter_provider import DeepSeekChapterProvider
from novel_agent.analysis.chapter_service import ChapterService
from novel_agent.analysis.contracts import AnalysisProvider
from novel_agent.analysis.deepseek import DeepSeekAnalysisProvider
from novel_agent.analysis.extraction_provider import DeepSeekExtractionProvider
from novel_agent.analysis.extraction_service import ExtractionService
from novel_agent.analysis.history import SourceAnalysisHistoryService
from novel_agent.analysis.jobs import (
    FullAnalysisJobInput,
    FullAnalysisJobService,
    FullAnalysisWorkflowGateway,
)
from novel_agent.analysis.memory_provider import DeepSeekMemoryProvider
from novel_agent.analysis.memory_service import MemoryService
from novel_agent.analysis.outline_provider import DeepSeekOutlineProvider
from novel_agent.analysis.outline_service import OutlineService
from novel_agent.analysis.rag_retriever import RagRetriever, SourceSearchRagRetriever
from novel_agent.analysis.service import SourceAnalysisService
from novel_agent.analysis.story_bible import StoryBibleService
from novel_agent.analysis.story_graph import StoryGraphService
from novel_agent.api.errors import (
    ProjectNotFoundError,
    project_conflict_exception_handler,
    project_not_found_exception_handler,
    source_exception_handler,
    unexpected_exception_handler,
    validation_exception_handler,
    workflow_not_found_exception_handler,
    workflow_service_unavailable_exception_handler,
)
from novel_agent.api.routes.analysis import router as analysis_router
from novel_agent.api.routes.chapter import router as chapter_router
from novel_agent.api.routes.extraction import router as extraction_router
from novel_agent.api.routes.health import InfrastructureReadinessProbe
from novel_agent.api.routes.health import router as health_router
from novel_agent.api.routes.memory import router as memory_router
from novel_agent.api.routes.outline import router as outline_router
from novel_agent.api.routes.projects import router as projects_router
from novel_agent.api.routes.search import (
    SearchProjection,
    SourceSearchGateway,
    SourceSearchService,
    SqlAlchemySearchAuthority,
    UnconfiguredSearchService,
)
from novel_agent.api.routes.search import router as search_router
from novel_agent.api.routes.sources import router as sources_router
from novel_agent.api.routes.story_bible import router as story_bible_router
from novel_agent.api.routes.story_graph import router as story_graph_router
from novel_agent.api.routes.workflows import router as workflows_router
from novel_agent.auth.contracts import PrincipalVerifier
from novel_agent.auth.verifier import OidcTokenVerifier
from novel_agent.config import Settings, get_settings
from novel_agent.db.session import create_engine, create_session_factory
from novel_agent.embeddings.ports import EmbeddingProvider
from novel_agent.graph.authority import SqlAlchemyGraphAuthority
from novel_agent.graph.projection import Neo4jGraphProjection
from novel_agent.graph.transport import (
    GraphProjectionError,
    create_official_neo4j_transport,
)
from novel_agent.projects.errors import ProjectConflictError
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
from novel_agent.workflows.client import (
    ProviderSourceIngestionWorkflowGateway,
    TemporalClientProvider,
    WorkflowNotFoundError,
    WorkflowServiceUnavailableError,
)
from novel_agent.workflows.full_analysis import (
    FullAnalysisInput,
    FullAnalysisWorkflow,
    full_analysis_workflow_id,
)


class _LazyFullAnalysisWorkflowGateway:
    """Lazy adapter that starts a Temporal workflow on first dispatch."""

    def __init__(self, provider: TemporalClientProvider, *, task_queue: str) -> None:
        self._provider = provider
        self._task_queue = task_queue

    async def dispatch(self, command: FullAnalysisJobInput, *, workflow_id: str) -> None:
        del workflow_id  # derived from job_id; kept for protocol compatibility
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.service import RPCError, RPCStatusCode

        client = await self._provider.get()
        wid = full_analysis_workflow_id(command.job_id)
        handle = client.get_workflow_handle(wid)
        try:
            description = await handle.describe()
        except RPCError as exc:
            if exc.status is not RPCStatusCode.NOT_FOUND:
                raise
        else:
            if description.status in (
                WorkflowExecutionStatus.RUNNING,
                WorkflowExecutionStatus.COMPLETED,
            ):
                return
        full_input = FullAnalysisInput(
            tenant_id=command.tenant_id,
            project_id=command.project_id,
            source_id=command.source_id,
            job_id=command.job_id,
        )
        try:
            await client.start_workflow(
                FullAnalysisWorkflow.run,
                full_input,
                id=wid,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        except WorkflowAlreadyStartedError:
            pass
        except RPCError as exc:
            raise WorkflowServiceUnavailableError from exc


def create_app(
    settings: Settings | None = None,
    *,
    oidc_verifier: PrincipalVerifier | None = None,
    object_store: object | None = None,
    source_ingestion_workflow_gateway: object | None = None,
    source_search_service: SourceSearchGateway | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    search_projection: SearchProjection | None = None,
    analysis_provider: AnalysisProvider | None = None,
    full_analysis_workflow_gateway: object | None = None,
) -> FastAPI:
    if source_search_service is not None and (
        embedding_provider is not None or search_projection is not None
    ):
        raise ValueError("source_search_service cannot be combined with search dependencies")
    if (embedding_provider is None) != (search_projection is None):
        raise ValueError("embedding_provider and search_projection must be supplied together")
    if (
        embedding_provider is not None
        and search_projection is not None
        and search_projection.embedding != embedding_provider.identity
    ):
        raise ValueError("search embedding identity differs from projection")
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved_settings)
        session_factory = create_session_factory(engine)
        temporal_client_provider = TemporalClientProvider(resolved_settings)
        app.state.session_factory = session_factory
        app.state.temporal_client_provider = temporal_client_provider
        readiness_probe = InfrastructureReadinessProbe(
            resolved_settings, session_factory, temporal_client_provider
        )
        app.state.readiness_probe = readiness_probe
        app.state.temporal_task_queue = resolved_settings.temporal_task_queue
        app.state.settings = resolved_settings
        app.state.object_store = object_store
        app.state.source_ingestion_workflow_gateway = (
            source_ingestion_workflow_gateway
            or ProviderSourceIngestionWorkflowGateway(
                temporal_client_provider,
                task_queue=resolved_settings.temporal_task_queue,
            )
        )
        if source_search_service is not None:
            app.state.source_search_service = source_search_service
        elif embedding_provider is not None and search_projection is not None:
            app.state.source_search_service = SourceSearchService(
                SqlAlchemySearchAuthority(session_factory), embedding_provider, search_projection
            )
        else:
            app.state.source_search_service = UnconfiguredSearchService()
        owned_verifier: OidcTokenVerifier | None = None
        verifier = oidc_verifier
        if verifier is None:
            owned_verifier = OidcTokenVerifier(
                resolved_settings.oidc_issuer,
                resolved_settings.oidc_audience,
            )
            verifier = owned_verifier
        app.state.oidc_verifier = verifier
        owned_analysis = (
            DeepSeekAnalysisProvider(resolved_settings) if analysis_provider is None else None
        )
        app.state.source_analysis_service = SourceAnalysisService(
            SqlAlchemyAnalysisAuthority(session_factory),
            cast(
                AnalysisProvider,
                analysis_provider if analysis_provider is not None else owned_analysis,
            ),
        )
        app.state.source_analysis_history_service = SourceAnalysisHistoryService(
            app.state.source_analysis_service, session_factory
        )
        app.state.story_bible_service = StoryBibleService(
            session_factory,
            cast(SourceAnalysisHistoryService, app.state.source_analysis_history_service),
        )
        graph_projection = None
        if (
            resolved_settings.neo4j_password is not None
            and resolved_settings.neo4j_uri
            and resolved_settings.neo4j_user
            and resolved_settings.neo4j_database
        ):
            try:
                transport = create_official_neo4j_transport(
                    uri=resolved_settings.neo4j_uri,
                    user=resolved_settings.neo4j_user,
                    password=resolved_settings.neo4j_password.get_secret_value(),
                    database=resolved_settings.neo4j_database,
                )
            except GraphProjectionError:
                transport = None
            if transport is not None:
                graph_projection = Neo4jGraphProjection(transport)
        app.state.graph_projection = graph_projection
        app.state.story_graph_service = StoryGraphService(
            session_factory,
            authority=SqlAlchemyGraphAuthority(session_factory),
            projection=cast(Neo4jGraphProjection | None, graph_projection),
        )
        # --- Extraction (Phase 3) ---
        owned_extraction_provider = DeepSeekExtractionProvider(resolved_settings)
        app.state.extraction_service = ExtractionService(
            cast(SourceAnalysisHistoryService, app.state.source_analysis_history_service),
            cast(StoryBibleService, app.state.story_bible_service),
            cast(StoryGraphService, app.state.story_graph_service),
            owned_extraction_provider,
        )
        # --- Outline (Phase 4) ---
        rag_retriever: RagRetriever | None = None
        if resolved_settings.rag_enabled and isinstance(
            app.state.source_search_service, SourceSearchService
        ):
            rag_retriever = SourceSearchRagRetriever(
                cast(SourceSearchService, app.state.source_search_service)
            )
        app.state.rag_retriever = rag_retriever
        owned_outline_provider = DeepSeekOutlineProvider(resolved_settings)
        app.state.outline_service = OutlineService(
            session_factory,
            cast(StoryBibleService, app.state.story_bible_service),
            cast(StoryGraphService, app.state.story_graph_service),
            owned_outline_provider,
            retriever=rag_retriever,
        )
        # --- Chapter (Phase 5) ---
        owned_chapter_provider = DeepSeekChapterProvider(resolved_settings)
        app.state.chapter_service = ChapterService(
            session_factory,
            cast(StoryBibleService, app.state.story_bible_service),
            cast(StoryGraphService, app.state.story_graph_service),
            cast(OutlineService, app.state.outline_service),
            owned_chapter_provider,
            retriever=rag_retriever,
        )
        # --- Memory (Phase 6) ---
        owned_memory_provider = DeepSeekMemoryProvider(resolved_settings)
        app.state.memory_service = MemoryService(
            session_factory,
            cast(StoryBibleService, app.state.story_bible_service),
            cast(StoryGraphService, app.state.story_graph_service),
            cast(OutlineService, app.state.outline_service),
            cast(ChapterService, app.state.chapter_service),
            owned_memory_provider,
        )
        full_analysis_provider = (
            analysis_provider if analysis_provider is not None else owned_analysis
        )
        app.state.full_analysis_gateway = (
            full_analysis_workflow_gateway
            or _LazyFullAnalysisWorkflowGateway(
                temporal_client_provider,
                task_queue=resolved_settings.temporal_task_queue,
            )
        )
        app.state.full_analysis_job_service = FullAnalysisJobService(
            session_factory,
            cast(AnalysisProvider, full_analysis_provider),
            cast(FullAnalysisWorkflowGateway, app.state.full_analysis_gateway),
        )
        try:
            yield
        finally:
            try:
                if owned_memory_provider is not None:
                    await owned_memory_provider.aclose()
            finally:
                try:
                    if owned_chapter_provider is not None:
                        await owned_chapter_provider.aclose()
                finally:
                    try:
                        if owned_outline_provider is not None:
                            await owned_outline_provider.aclose()
                    finally:
                        try:
                            if owned_extraction_provider is not None:
                                await owned_extraction_provider.aclose()
                        finally:
                            try:
                                if owned_analysis is not None:
                                    await owned_analysis.aclose()
                            finally:
                                try:
                                    if graph_projection is not None:
                                        await graph_projection.aclose()
                                finally:
                                    try:
                                        await readiness_probe.aclose()
                                    finally:
                                        try:
                                            if owned_verifier is not None:
                                                await owned_verifier.aclose()
                                        finally:
                                            await engine.dispose()

    app = FastAPI(title=resolved_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(sources_router, prefix="/api/v1")
    app.include_router(workflows_router, prefix="/api/v1")
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(analysis_router, prefix="/api/v1")
    app.include_router(story_bible_router, prefix="/api/v1")
    app.include_router(story_graph_router, prefix="/api/v1")
    app.include_router(extraction_router, prefix="/api/v1")
    app.include_router(outline_router, prefix="/api/v1")
    app.include_router(chapter_router, prefix="/api/v1")
    app.include_router(memory_router, prefix="/api/v1")
    _mount_web_frontend(app, resolved_settings)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ProjectNotFoundError, project_not_found_exception_handler)
    app.add_exception_handler(ProjectConflictError, project_conflict_exception_handler)
    for source_error in (
        SourceNotFound,
        InvalidSourceCursor,
        UploadIntentExpired,
        UploadValidationInProgress,
        StorageUnavailable,
        CorruptSource,
        SourceConflictError,
        SourceHashMismatchError,
        SourceSizeLimitError,
        UnsupportedSourceTypeError,
        AuthorizationProvenanceError,
        UploadQuotaExceededError,
    ):
        app.add_exception_handler(source_error, source_exception_handler)
    app.add_exception_handler(WorkflowNotFoundError, workflow_not_found_exception_handler)
    app.add_exception_handler(
        WorkflowServiceUnavailableError, workflow_service_unavailable_exception_handler
    )
    app.add_exception_handler(Exception, unexpected_exception_handler)
    return app


def _mount_web_frontend(app: FastAPI, settings: Settings) -> None:
    """挂载 web/dist 静态前端（Vue 构建产物）。目录不存在时静默跳过。"""
    web_dist = getattr(settings, "web_dist_dir", "") or "web/dist"
    dist = Path(web_dist)
    if not dist.is_dir():
        return
    app.mount(
        "/",
        StaticFiles(directory=str(dist), html=True),
        name="web",
    )


app = create_app()
