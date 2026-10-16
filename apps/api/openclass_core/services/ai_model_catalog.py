"""Portable model capabilities; pricing and access policy are supplied by a host."""
from openclass_core.models import AIModelSelection
from openclass_core.services.studio_ports import host_service
OPENAI_CODEX_REALTIME_MODEL = 'gpt-live-1-codex'
OPENAI_DEFAULT_REALTIME_MODEL = 'gpt-realtime-2.1'

def build_model_catalog(user_id, connection_id=None):
    return host_service('model_catalog', user_id, connection_id=connection_id)

async def build_model_catalog_with_pricing(user_id):
    return build_model_catalog(user_id)

def default_text_selection(**kwargs):
    return host_service('default_text_selection', **kwargs)

def resolve_text_model_selection(selection, *, user_id, catalog_builder=None):
    return host_service('resolve_model_selection', selection, user_id=user_id, catalog_builder=catalog_builder)

def text_model_supports_image_inputs(selection, *, user_id, catalog_builder=None, frozen_capabilities=None):
    if frozen_capabilities is not None:
        from openclass_core.services.model_capability_snapshot import frozen_image_capability
        return frozen_image_capability(selection, user_id, frozen_capabilities)
    try:
        catalog = catalog_builder(user_id) if catalog_builder else build_model_catalog(user_id, selection.connection_id)
    except Exception:
        return False
    matches = [item for item in catalog.text if item.provider == selection.provider and item.model == selection.model and (selection.connection_id is None or item.connection_id == selection.connection_id) and (selection.access_method is None or item.access_method == selection.access_method)]
    return bool(matches and all((item.supports_image_inputs for item in matches)))

def default_realtime_selection():
    return host_service('default_realtime_selection')

def realtime_runtime_enabled():
    return host_service('realtime_enabled')

def codex_realtime_runtime_enabled():
    return host_service('codex_realtime_enabled')

def codex_realtime_proxy_configured():
    return host_service('codex_realtime_configured')

def codex_realtime_user_allowed(user_id):
    return host_service('codex_realtime_allowed', user_id)
