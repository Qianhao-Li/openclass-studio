from __future__ import annotations
import json
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter
GeometryDimension = Literal['2d', '3d']
GeometryPrimitiveKind = Literal['point', 'segment', 'line', 'polyline', 'polygon', 'circle', 'ellipse', 'vector', 'plane', 'sphere', 'label']

class GeometryPoint(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(default='', max_length=64)
    x: float = Field(ge=-1000000, le=1000000)
    y: float = Field(ge=-1000000, le=1000000)
    z: float = Field(default=0, ge=-1000000, le=1000000)
    color: str = Field(default='#0f172a', max_length=32)
    hidden: bool = False

class GeometryPrimitive(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    kind: GeometryPrimitiveKind
    label: str = Field(default='', max_length=96)
    point_ids: list[str] = Field(default_factory=list, max_length=96)
    center_id: str = Field(default='', max_length=64)
    radius: float | None = Field(default=None, gt=0, le=1000000)
    radius_y: float | None = Field(default=None, gt=0, le=1000000)
    text: str = Field(default='', max_length=240)
    color: str = Field(default='#2563eb', max_length=32)
    fill: str = Field(default='none', max_length=32)
    opacity: float = Field(default=1, ge=0, le=1)
    stroke_width: float = Field(default=2, ge=0.5, le=12)
    dashed: bool = False

class GeometryViewport(BaseModel):
    x_min: float = Field(default=-6, ge=-1000000, le=1000000)
    x_max: float = Field(default=6, ge=-1000000, le=1000000)
    y_min: float = Field(default=-4, ge=-1000000, le=1000000)
    y_max: float = Field(default=4, ge=-1000000, le=1000000)

    @model_validator(mode='after')
    def validate_bounds(self) -> 'GeometryViewport':
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError('Geometry viewport bounds must be increasing')
        return self

class GeometryScene(BaseModel):
    version: Literal['1.0'] = '1.0'
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(default='', max_length=600)
    dimension: GeometryDimension = '2d'
    show_axes: bool = True
    show_grid: bool = True
    viewport: GeometryViewport = Field(default_factory=GeometryViewport)
    points: list[GeometryPoint] = Field(default_factory=list, max_length=96)
    primitives: list[GeometryPrimitive] = Field(default_factory=list, max_length=192)
    steps: list[str] = Field(default_factory=list, max_length=12)
    source_excerpt: str = Field(default='', max_length=6000)

    @model_validator(mode='after')
    def validate_scene_graph(self) -> 'GeometryScene':
        point_ids = [point.id for point in self.points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError('Geometry point ids must be unique')
        primitive_ids = [primitive.id for primitive in self.primitives]
        if len(primitive_ids) != len(set(primitive_ids)):
            raise ValueError('Geometry primitive ids must be unique')
        known_points = set(point_ids)
        for primitive in self.primitives:
            referenced = [*primitive.point_ids]
            if primitive.center_id:
                referenced.append(primitive.center_id)
            missing = [point_id for point_id in referenced if point_id not in known_points]
            if missing:
                raise ValueError(f'Geometry primitive {primitive.id} references missing points')
            if primitive.kind in {'segment', 'line', 'vector'} and len(primitive.point_ids) < 2:
                raise ValueError(f'Geometry primitive {primitive.id} requires two points')
            if primitive.kind in {'polyline', 'polygon', 'plane'} and len(primitive.point_ids) < 3:
                raise ValueError(f'Geometry primitive {primitive.id} requires at least three points')
            if primitive.kind in {'circle', 'sphere'} and (not primitive.center_id or primitive.radius is None):
                raise ValueError(f'Geometry primitive {primitive.id} requires a center and radius')
            if primitive.kind == 'ellipse' and (not primitive.center_id or primitive.radius is None or primitive.radius_y is None):
                raise ValueError(f'Geometry primitive {primitive.id} requires a center and two radii')
            if primitive.kind in {'point', 'label'} and (not primitive.point_ids):
                raise ValueError(f'Geometry primitive {primitive.id} requires an anchor point')
        if not self.points or not self.primitives:
            raise ValueError('Geometry scene must include points and primitives')
        return self
GEOMETRY_SCENE_INSTRUCTIONS = '\nYou are the geometry-scene adapter for an interactive learning workspace.\n\nTransform one quoted board excerpt into a compact, accurate scene graph. The excerpt may contain a\nformula, a problem statement, or both. Infer only the objects supported by the excerpt. Choose 2d or\n3d from the described relationships. Use finite Cartesian coordinates that preserve the important\nincidence, parallel, perpendicular, tangent, symmetry, or solid relationships. Labels should match\nthe quoted notation when possible.\n\nThe request may also include backend-verified attachment text and image inputs explicitly selected\nfor this generation. Use them as supporting evidence for the quoted excerpt, including diagrams,\nconstraints, labels, and measurements that are visible in the attachment. Do not infer content from\nunselected files or unrelated course materials. If the attachment and excerpt conflict, keep the\nexcerpt as the generation scope and disclose the ambiguity in summary.\n\nReturn only the supplied GeometryScene contract. Never return HTML, JavaScript, SVG, Markdown, or\nexecutable expressions. Do not invent a fixed lesson template. If the excerpt leaves scale or\norientation free, choose a simple representative configuration and disclose that choice in summary.\nKeep steps short and explain how the visible objects correspond to the quoted content; do not solve\nthe whole exercise. Use stable ASCII ids. Prefer segments and polygons for bounded objects, lines for\nunbounded relationships, and planes only for 3d scenes.\n'.strip()

def generate_geometry_scene(*, adapter: AIExecutionAdapter, source_excerpt: str, instructions: str='', attachment_context: str='', image_inputs: list[str] | None=None) -> GeometryScene:
    normalized_excerpt = source_excerpt.strip()
    if not normalized_excerpt:
        raise ValueError('A non-empty board excerpt is required')
    response = adapter.parse_structured(system_prompt=GEOMETRY_SCENE_INSTRUCTIONS, user_prompt=json.dumps({'board_excerpt': normalized_excerpt, 'user_guidance': instructions.strip(), 'verified_attachment_context': attachment_context.strip(), 'response_contract': GeometryScene.model_json_schema()}, ensure_ascii=False), schema=GeometryScene, image_inputs=image_inputs)
    scene = GeometryScene.model_validate(response.output_parsed)
    return scene.model_copy(update={'source_excerpt': normalized_excerpt})
