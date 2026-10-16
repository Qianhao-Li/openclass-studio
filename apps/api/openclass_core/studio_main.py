"""Local Studio ASGI entrypoint; no platform router or platform worker is mounted."""
from __future__ import annotations
from contextlib import asynccontextmanager

def create_app(config=None):
    from openclass_core.services.studio_runtime_config import StudioRuntimeConfig
    config = config or StudioRuntimeConfig.from_env()
    config.configure_environment()
    from openclass_core.services.studio_state import StudioStateStore
    from openclass_core.services.studio_ports import configure_studio_services
    state = StudioStateStore(config.data_root)
    configure_studio_services({'connections': state.connections, 'local_state': lambda: state, 'preferred_connection': lambda _store, owner: state.preferences(owner)['settings']['preferred_codex_connection_id'], 'recover_workspace_pull': lambda *_args, **_kwargs: None, 'source_preferences_store': lambda _path: state, 'source_registered': lambda _store, _owner: False, 'credit_billing_enabled': lambda: False})
    from openclass_core.services.studio_personal_models import local_model_services
    configure_studio_services(local_model_services())
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool
    from openclass_core.services.studio_identity import LocalStudioIdentity
    from openclass_core.routers import workspace, documents, chat, document_history, history_deletion, history_edit, history_order, lesson_merges, sources, source_evidence_snapshots, geometry, workspace_routes, studio_bootstrap, studio_courses, studio_models, studio_model_accounts, studio_source_preferences, studio_connection, studio_sync
    from openclass_core import studio_dependencies as auth
    from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error
    from openclass_core.services.ridoc_runtime_recovery import recover_interrupted_runtime_turns
    from openclass_core.services.workspace_state import ensure_data_dirs, get_ridoc_workspace_store
    identity = LocalStudioIdentity(config.data_root, launch_token=config.launch_token, web_origin=config.web_origin, api_origin=config.api_origin)
    configure_studio_services({'local_profile': lambda: identity.principal})
    ensure_data_dirs()

    @asynccontextmanager
    async def lifespan(_app):
        from openclass_core.services.studio_runtime_lock import studio_writer_lock
        with studio_writer_lock(config.data_root):
            await run_in_threadpool(recover_interrupted_runtime_turns, get_ridoc_workspace_store())
            yield
    app = FastAPI(title='OpenClass Studio', version='0.1.0', lifespan=lifespan)
    app.state.local_studio_identity = identity
    app.state.studio_identity = identity.resolve
    app.state.studio_websocket_identity = identity.resolve
    app.state.studio_state = state
    app.dependency_overrides[auth.current_user] = identity.resolve
    app.dependency_overrides[auth.current_websocket_user] = identity.resolve

    @app.middleware('http')
    async def guard_local_transport(request: Request, call_next):
        try:
            if request.url.path == '/api/workbench/v1/session':
                identity.open_session(request)
            elif request.method == 'OPTIONS' or request.url.path == '/health':
                identity.check_transport(request)
            else:
                identity.resolve(request)
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={'detail': error.detail})
        return await call_next(request)
    app.add_middleware(CORSMiddleware, allow_origins=[config.web_origin], allow_credentials=True, allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'], allow_headers=['Content-Type', 'X-OpenClass-Launch-Token', 'X-OpenClass-History-View', 'Accept'])

    @app.exception_handler(RidocV3Error)
    async def ridoc_failure(_request, error):
        return JSONResponse(status_code=409 if isinstance(error, RidocV3ConflictError) else 422, content={'detail': str(error)})

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': 'local', 'protocol_version': '1'}
    app.include_router(studio_bootstrap.router)
    app.include_router(studio_models.router)
    app.include_router(studio_model_accounts.router)
    app.include_router(studio_source_preferences.router)
    app.include_router(studio_connection.router)
    app.include_router(studio_sync.router)
    from openclass_core.workbench_routes import mount_workbench_routes
    mount_workbench_routes(app, (workspace.router, documents.router, chat.router, document_history.router, history_deletion.router, history_edit.router, history_order.router, lesson_merges.router, sources.router, source_evidence_snapshots.router, geometry.router, workspace_routes.router, studio_courses.router), workspace_router=workspace.router, dependencies=[Depends(identity.resolve)], compatibility=True)
    return app
