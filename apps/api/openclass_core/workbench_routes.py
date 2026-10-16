"""One public route boundary for local and hosted Studio composition."""
from fastapi.routing import APIRoute
WORKSPACE_PATHS = {'/api/workspace', '/api/workspace/tabs', '/api/workspace/summary', '/api/workspace/activity', '/api/course-package', '/api/workspace/import-ridoc', '/api/workspace/reorder', '/api/packages', '/api/packages/{package_id}/open', '/api/packages/{package_id}/delete', '/api/lessons/generate', '/api/lessons/{lesson_id}/open', '/api/lessons/{lesson_id}/close', '/api/lessons/{lesson_id}/rename', '/api/lessons/{lesson_id}/delete', '/api/lessons/{lesson_id}/head', '/api/lessons/{lesson_id}/move', '/api/lessons/batch', '/api/account/recently-deleted', '/api/account/recently-deleted/{entry_id}/restore'}
COURSE_PREFIXES = ('/api/lessons/', '/api/packages/', '/api/documents/', '/api/board-assets/', '/api/geometry/', '/api/workspace/routes')

def mount_workbench_routes(app, routers, *, workspace_router, dependencies=(), compatibility=False):
    for source in routers:
        for route in source.routes:
            if not isinstance(route, APIRoute):
                continue
            if source is workspace_router:
                if route.path not in WORKSPACE_PATHS:
                    continue
            elif route.path != '/api/source-preferences' and (not route.path.startswith(COURSE_PREFIXES)):
                continue
            prefixes = ('', '/api/workbench/v1') if compatibility else ('/api/workbench/v1',)
            for prefix in prefixes:
                path = prefix + route.path.removeprefix('/api') if prefix else route.path
                app.add_api_route(path, route.endpoint, methods=list(route.methods), response_model=route.response_model, status_code=route.status_code, response_class=route.response_class, dependencies=[*dependencies, *route.dependencies], name=('workbench_' if prefix else 'compat_') + route.name)
