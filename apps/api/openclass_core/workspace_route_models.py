"""Browser addresses for the owner's editable workspace, outside course history."""
from pydantic import BaseModel

class WorkspaceRouteView(BaseModel):
    canonical_path: str
    package_id: str
    lesson_id: str
