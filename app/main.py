from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.audit import (
    correlation_id,
    emit_audit_event,
    emit_audit_record,
    request_audit_context,
)
from app.adapters.google_workspace.docs import GoogleDocsAdapter
from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    DriveListResponse,
    GoogleDocContent,
    SheetRangeContent,
    SourceMetadata,
    WorkspaceStatusResponse,
)
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.adapters.hubspot.mcp_client import account_metadata_from_result
from app.adapters.hubspot.models import HubSpotConnectionStatus
from app.adapters.hubspot.runtime import HubSpotRuntime, get_hubspot_runtime
from app.config.settings import Settings, get_settings
from app.knowledge import (
    discover_candidate_sources,
    list_authorized_source_files,
    list_registered_sources,
    registered_source,
    retrieve_authorized_document,
    retrieve_authorized_sheet_range_by_id,
)
from app.mcp_server import mcp_http_app, mcp_server
from app.middleware.authentication import GatewayAuthenticationMiddleware
from app.policies.workspace import ContentReadPolicy, DriveReadPolicy
from app.policies.source_access import SourceAccessPolicy
from app.source_discovery.google_workspace import GoogleWorkspaceSourceDiscovery
from app.source_discovery.interface import (
    DiscoveryResponse,
    SourceDiscovery,
)
from app.source_registry import SourceRegistry, SourceRegistryMetadata
from app.auth.pubsub_push import parse_pubsub_signal, verify_pubsub_oidc
from app.agent_signals import AgentSignalInbox
from app.runtime import get_runtime_gateway


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(
    title="Brunova Knowledge Gateway",
    version="0.30.1",
    lifespan=lifespan,
)
app.add_middleware(GatewayAuthenticationMiddleware)


def _get_valid_settings() -> Settings:
    try:
        return get_settings()
    except ValueError as error:
        raise WorkspaceAdapterError(
            "configuration_invalid",
            str(error),
            503,
        ) from error


def get_workspace_adapter() -> GoogleWorkspaceAdapter:
    return GoogleWorkspaceAdapter(_get_valid_settings())


def get_docs_adapter() -> GoogleDocsAdapter:
    return GoogleDocsAdapter(_get_valid_settings())


def get_sheets_adapter() -> GoogleSheetsAdapter:
    return GoogleSheetsAdapter(_get_valid_settings())


def get_hubspot_adapter_runtime() -> HubSpotRuntime:
    try:
        return get_hubspot_runtime()
    except ValueError as error:
        raise WorkspaceAdapterError(
            "hubspot_configuration_invalid",
            "HubSpot integration is not configured.",
            503,
        ) from error


def get_source_registry() -> SourceRegistry:
    settings = _get_valid_settings()
    try:
        return _load_source_registry(settings.workspace_source_registry_path)
    except ValueError as error:
        raise WorkspaceAdapterError(
            "source_registry_invalid",
            "Source registry configuration is invalid.",
            503,
        ) from error


@lru_cache
def _load_source_registry(path: str) -> SourceRegistry:
    return SourceRegistry.load(path)


def get_source_policy() -> SourceAccessPolicy:
    try:
        return SourceAccessPolicy(_get_valid_settings(), get_source_registry())
    except ValueError as error:
        raise WorkspaceAdapterError("configuration_invalid", str(error), 503) from error


WorkspaceAdapter = Annotated[GoogleWorkspaceAdapter, Depends(get_workspace_adapter)]
DocsAdapter = Annotated[GoogleDocsAdapter, Depends(get_docs_adapter)]
SheetsAdapter = Annotated[GoogleSheetsAdapter, Depends(get_sheets_adapter)]
HubSpotAdapterRuntime = Annotated[
    HubSpotRuntime, Depends(get_hubspot_adapter_runtime)
]
SourcePolicy = Annotated[SourceAccessPolicy, Depends(get_source_policy)]
Registry = Annotated[SourceRegistry, Depends(get_source_registry)]


def get_source_discovery(
    registry: Registry,
    adapter: WorkspaceAdapter,
) -> GoogleWorkspaceSourceDiscovery:
    settings = _get_valid_settings()
    return GoogleWorkspaceSourceDiscovery(
        adapter,
        registry,
        blocked_location_ids=settings.workspace_blocked_source_ids,
    )


Discovery = Annotated[SourceDiscovery, Depends(get_source_discovery)]


@app.middleware("http")
async def correlation_and_audit(request: Request, call_next):
    request.state.request_id = correlation_id(request.headers.get("X-Correlation-ID"))
    try:
        response = await call_next(request)
    except Exception:
        action, resource_id, resource_type = request_audit_context(request)
        if action:
            emit_audit_event(
                request,
                action=action,
                resource_id=resource_id,
                resource_type=resource_type,
                result="error",
                http_status=500,
                error_code="unhandled_error",
            )
        raise
    response.headers["X-Correlation-ID"] = request.state.request_id
    action, resource_id, resource_type = request_audit_context(request)
    if action:
        result = (
            "success"
            if response.status_code < 400
            else "rejected"
            if response.status_code < 500
            else "error"
        )
        emit_audit_event(
            request,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result=result,
            http_status=response.status_code,
            error_code=getattr(request.state, "error_code", None),
        )
    return response


@app.exception_handler(WorkspaceAdapterError)
async def workspace_adapter_error_handler(
    request: Request, error: WorkspaceAdapterError
) -> JSONResponse:
    request.state.error_code = error.code
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {"code": error.code, "message": error.message},
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, error: HTTPException) -> JSONResponse:
    request.state.error_code = "policy_validation_failed"
    return JSONResponse(
        status_code=error.status_code,
        content={
            "detail": jsonable_encoder(error.detail),
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    request.state.error_code = "invalid_request"
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(error.errors()),
            "request_id": request.state.request_id,
        },
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "brunova-knowledge-gateway"
    }


@app.get("/identity")
def identity():
    return {
        "service_account": "brunova-knowledge-agent",
        "environment": "cloud-run"
    }


@app.get("/capabilities")
def capabilities():
    return {
        "capabilities": [
            "google_workspace",
            "source_registry",
            "source_discovery",
            "source_governance",
            "mcp_read",
            "gateway_authentication",
            "hubspot_governed_mcp",
            "n8n_full_access_mcp",
            "agent_signal_inbox",
        ]
    }


def get_agent_signal_inbox() -> AgentSignalInbox:
    inbox = get_runtime_gateway().agent_signal_inbox
    if inbox is None:
        raise WorkspaceAdapterError(
            "agent_signal_inbox_unavailable",
            "Agent Signal Inbox is not configured.",
            503,
        )
    return inbox


@app.post("/events/agent-signals", status_code=204)
async def receive_agent_signal(request: Request) -> Response:
    """Receive only authenticated Pub/Sub push deliveries and durably ack them."""

    request_id = request.state.request_id
    try:
        verify_pubsub_oidc(request.headers.get("Authorization"))
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action="agent_signal_received",
            resource_id=None,
            resource_type="agent_signal",
            result="rejected" if error.status_code < 500 else "error",
            http_status=error.status_code,
            error_code=error.code,
            provider="agent_signals",
            principal_id="pubsub_push",
            principal_type="service",
            include_active_principal=False,
        )
        raise
    try:
        delivery = await parse_pubsub_signal(request)
    except WorkspaceAdapterError as error:
        # Permanent payload failures are acknowledged to stop infinite retries.
        # The safe audit record intentionally contains no payload fields.
        emit_audit_record(
            request_id=request_id,
            action="agent_signal_received",
            resource_id=None,
            resource_type="agent_signal",
            result="rejected",
            http_status=204,
            error_code=error.code,
            provider="agent_signals",
            principal_id="pubsub_push",
            principal_type="service",
            include_active_principal=False,
        )
        return Response(status_code=204)
    try:
        result = get_agent_signal_inbox().receive(
            delivery.payload,
            message_id=delivery.message_id,
            publish_time=delivery.publish_time,
            attributes=delivery.attributes,
        )
    except Exception as error:
        code = (
            error.code
            if isinstance(error, WorkspaceAdapterError)
            else "agent_signal_persistence_failed"
        )
        status = (
            error.status_code if isinstance(error, WorkspaceAdapterError) else 503
        )
        emit_audit_record(
            request_id=request_id,
            action="agent_signal_received",
            resource_id=delivery.payload.signal_id,
            resource_type="agent_signal",
            result="error",
            http_status=status,
            error_code=code,
            provider="agent_signals",
            signal_id=delivery.payload.signal_id,
            signal_type=delivery.payload.signal_type,
            principal_id="pubsub_push",
            principal_type="service",
            include_active_principal=False,
        )
        if isinstance(error, WorkspaceAdapterError):
            raise
        raise WorkspaceAdapterError(
            "agent_signal_persistence_failed",
            "Agent Signal could not be persisted.",
            503,
        ) from error
    emit_audit_record(
        request_id=request_id,
        action="agent_signal_received",
        resource_id=result.signal_id,
        resource_type="agent_signal",
        result="success",
        http_status=204,
        provider="agent_signals",
        signal_id=result.signal_id,
        signal_type=result.signal_type,
        status_transition="received->pending" if result.created else "deduplicated",
        principal_id="pubsub_push",
        principal_type="service",
        include_active_principal=False,
    )
    return Response(status_code=204)


@app.get("/auth/hubspot/connect")
async def hubspot_connect(runtime: HubSpotAdapterRuntime) -> RedirectResponse:
    authorization_url = await runtime.oauth.begin_authorization()
    return RedirectResponse(
        authorization_url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/auth/hubspot/callback", response_class=HTMLResponse)
async def hubspot_callback(
    state: Annotated[str, Query(min_length=32, max_length=512)],
    runtime: HubSpotAdapterRuntime,
    code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> HTMLResponse:
    if error or not code:
        runtime.token_store.consume_pending(state)
        raise WorkspaceAdapterError(
            "hubspot_oauth_denied", "HubSpot authorization was not completed.", 400
        )
    token = await runtime.oauth.exchange_code(code=code, state=state)
    runtime.token_manager.remember(token)
    try:
        details = await runtime.mcp_client.call_tool("get_user_details", {})
        runtime.token_store.update_account(account_metadata_from_result(details))
    except Exception:
        # The OAuth connection remains valid if downstream discovery is transiently
        # unavailable. Status and the governed MCP tools can retry it safely.
        pass
    return HTMLResponse(
        "<!doctype html><html><body><h1>HubSpot conectado</h1>"
        "<p>La conexión se completó. Puedes cerrar esta ventana.</p></body></html>",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/auth/hubspot/status", response_model=HubSpotConnectionStatus)
def hubspot_status(runtime: HubSpotAdapterRuntime) -> HubSpotConnectionStatus:
    return runtime.token_store.status()


@app.get("/workspace/status", response_model=WorkspaceStatusResponse)
def workspace_status(request: Request, adapter: WorkspaceAdapter) -> WorkspaceStatusResponse:
    adapter.check_connection()
    return WorkspaceStatusResponse.connected(
        adapter.delegated_user, request.state.request_id
    )


@app.get(
    "/workspace/drive/list",
    response_model=DriveListResponse,
    deprecated=True,
)
def workspace_drive_list(
    request: Request,
    adapter: WorkspaceAdapter,
    source_policy: SourcePolicy,
    limit: Annotated[int, Query(ge=1)] = 10,
) -> DriveListResponse:
    safe_limit = DriveReadPolicy.validate_list_limit(limit)
    files = adapter.list_files(limit=safe_limit, source_policy=source_policy)
    if files:
        source_ids = {item.source.id for item in files}
        classifications = {item.source.classification for item in files}
        request.state.source_id = (
            next(iter(source_ids)) if len(source_ids) == 1 else sorted(source_ids)
        )
        request.state.classification = (
            next(iter(classifications)).value
            if len(classifications) == 1
            else sorted(item.value for item in classifications)
        )
    return DriveListResponse(
        files=files,
        request_id=request.state.request_id,
    )


@app.get("/sources", response_model=list[SourceRegistryMetadata])
def list_sources(registry: Registry) -> list[SourceRegistryMetadata]:
    return list_registered_sources(registry)


@app.get("/sources/discover", response_model=DiscoveryResponse)
def discover_sources(
    request: Request,
    discovery: Discovery,
    limit: Annotated[int, Query(ge=1)] = 25,
) -> DiscoveryResponse:
    result = discover_candidate_sources(
        source_discovery=discovery,
        limit=limit,
    )
    request.state.candidate_count = len(result.candidates)
    return DiscoveryResponse.from_result(result)


@app.get("/sources/{source_id}", response_model=SourceRegistryMetadata)
def get_source(source_id: str, registry: Registry) -> SourceRegistryMetadata:
    return SourceRegistryMetadata.from_definition(
        registered_source(registry, source_id)
    )


@app.get("/sources/{source_id}/files", response_model=DriveListResponse)
def source_files(
    request: Request,
    source_id: str,
    registry: Registry,
    adapter: WorkspaceAdapter,
    source_policy: SourcePolicy,
    limit: Annotated[int, Query(ge=1)] = 10,
) -> DriveListResponse:
    request.state.source_id = source_id
    source = registered_source(registry, source_id)
    request.state.classification = source.classification.value
    _, files = list_authorized_source_files(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=adapter,
        source_id=source_id,
        limit=limit,
    )
    return DriveListResponse(
        files=files,
        request_id=request.state.request_id,
    )


@app.get("/sources/{source_id}/docs/{document_id}", response_model=GoogleDocContent)
def source_document(
    request: Request,
    source_id: str,
    document_id: str,
    registry: Registry,
    workspace_adapter: WorkspaceAdapter,
    adapter: DocsAdapter,
    source_policy: SourcePolicy,
) -> GoogleDocContent:
    request.state.source_id = source_id
    source = registered_source(registry, source_id)
    request.state.classification = source.classification.value
    _, result = retrieve_authorized_document(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        docs_adapter=adapter,
        source_id=source_id,
        document_id=document_id,
    )
    return result.model_copy(update={"request_id": request.state.request_id})


@app.get(
    "/sources/{source_id}/sheets/{spreadsheet_id}",
    response_model=SheetRangeContent,
)
def source_sheet_range(
    request: Request,
    source_id: str,
    spreadsheet_id: str,
    registry: Registry,
    workspace_adapter: WorkspaceAdapter,
    adapter: SheetsAdapter,
    source_policy: SourcePolicy,
    range_: Annotated[str, Query(alias="range", min_length=1, max_length=500)],
) -> SheetRangeContent:
    request.state.source_id = source_id
    source = registered_source(registry, source_id)
    request.state.classification = source.classification.value
    _, result = retrieve_authorized_sheet_range_by_id(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        sheets_adapter=adapter,
        source_id=source_id,
        spreadsheet_id=spreadsheet_id,
        range_name=range_,
    )
    return result.model_copy(update={"request_id": request.state.request_id})


@app.get(
    "/workspace/docs/{document_id}",
    response_model=GoogleDocContent,
    deprecated=True,
)
def workspace_document(
    request: Request,
    document_id: str,
    workspace_adapter: WorkspaceAdapter,
    adapter: DocsAdapter,
    source_policy: SourcePolicy,
) -> GoogleDocContent:
    safe_id = ContentReadPolicy.validate_resource_id(document_id)
    ContentReadPolicy.validate_document_limit(adapter.max_chars)
    resource = workspace_adapter.get_resource(safe_id)
    source_context = source_policy.authorize(resource)
    request.state.source_id = source_context.source_id
    request.state.classification = source_context.classification.value
    result = adapter.get_document(resource, max_chars=adapter.max_chars)
    return result.model_copy(
        update={
            "request_id": request.state.request_id,
            "source": SourceMetadata(
                id=source_context.source_id,
                name=source_context.source_name,
                classification=source_context.classification.value,
            ),
        }
    )


@app.get(
    "/workspace/sheets/{spreadsheet_id}",
    response_model=SheetRangeContent,
    deprecated=True,
)
def workspace_sheet_range(
    request: Request,
    spreadsheet_id: str,
    workspace_adapter: WorkspaceAdapter,
    adapter: SheetsAdapter,
    source_policy: SourcePolicy,
    range_: Annotated[str, Query(alias="range", min_length=1, max_length=500)],
) -> SheetRangeContent:
    safe_id = ContentReadPolicy.validate_resource_id(spreadsheet_id)
    safe_range = ContentReadPolicy.validate_sheet_range(
        range_, max_cells=adapter.max_cells
    )
    resource = workspace_adapter.get_resource(safe_id)
    source_context = source_policy.authorize(resource)
    request.state.source_id = source_context.source_id
    request.state.classification = source_context.classification.value
    result = adapter.get_range(resource, range_name=safe_range)
    return result.model_copy(
        update={
            "request_id": request.state.request_id,
            "source": SourceMetadata(
                id=source_context.source_id,
                name=source_context.source_name,
                classification=source_context.classification.value,
            ),
        }
    )


app.mount("/mcp", mcp_http_app)
