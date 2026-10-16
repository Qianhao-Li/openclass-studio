from fastapi import APIRouter, Depends, Query, Response
from openclass_core.models import UserView
from openclass_core.studio_dependencies import current_user
from openclass_core.services.workspace_routes import resolve_route, route_for_lesson
from openclass_core.workspace_route_models import WorkspaceRouteView
router = APIRouter(prefix='/api/workspace/routes', tags=['workspace'])

@router.get('', response_model=WorkspaceRouteView)
def workspace_route(response: Response, lesson_id: str | None=Query(default=None, min_length=1), user: UserView=Depends(current_user)) -> WorkspaceRouteView:
    response.headers['Cache-Control'] = 'no-store'
    return route_for_lesson(user.id, lesson_id)

@router.get('/{course_slug}', response_model=WorkspaceRouteView)
def workspace_route_by_slug(course_slug: str, response: Response, lesson_slug: str | None=Query(default=None, min_length=1), user: UserView=Depends(current_user)) -> WorkspaceRouteView:
    response.headers['Cache-Control'] = 'no-store'
    return resolve_route(user.id, course_slug, lesson_slug)

@router.get('/{course_slug}/{lesson_slug}', response_model=WorkspaceRouteView)
def workspace_lesson_route(course_slug: str, lesson_slug: str, response: Response, user: UserView=Depends(current_user)) -> WorkspaceRouteView:
    response.headers['Cache-Control'] = 'no-store'
    return resolve_route(user.id, course_slug, lesson_slug)
