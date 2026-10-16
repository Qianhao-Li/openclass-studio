"""Only the current workspace's explicitly configured model access is exposed."""
from openclass_core.models import AIModelCatalog, AIModelOption, AIModelSelection, AIReasoningEffortOption, AIAgentBackendOption
from openclass_core.services.pi_provider_catalog import pi_api_provider_catalog
from openclass_core.services.pi_agent_runtime import pi_personal_api_configured, pi_runtime_available
from openclass_core.services.codex_model_catalog import CODEX_TEXT_MODELS, DEFAULT_CODEX_TEXT_MODEL

def personal_model_catalog(owner: str, connection_id: str | None=None) -> AIModelCatalog:
    runtime = pi_runtime_available()
    options = []
    for provider in pi_api_provider_catalog():
        configured = runtime and pi_personal_api_configured(owner_user_id=owner, provider=provider['provider'])
        if not configured:
            continue
        for model in provider['models']:
            options.append(AIModelOption(provider=provider['provider'], model=model['model'], access_method='personal_api', label=model.get('label') or model['model'], capability='text', configured=configured, enabled=configured, supports_image_inputs=model.get('supports_image_inputs', False), default_reasoning_effort='medium' if 'medium' in model.get('reasoning_efforts', []) else None, supported_reasoning_efforts=[AIReasoningEffortOption(reasoning_effort=value) for value in model.get('reasoning_efforts', [])]))
    subscription_ready = False
    if connection_id:
        from openclass_core.services.codex_connections import resolve_codex_lease
        from openclass_core.services.pi_agent_runtime import pi_credentials_available
        resolve_codex_lease(owner, connection_id)
        subscription_ready = runtime and pi_credentials_available(owner_user_id=owner, connection_id=connection_id)
    for model, label in CODEX_TEXT_MODELS:
        options.append(AIModelOption(provider='openai_codex', model=model, access_method='chatgpt_subscription', connection_id=connection_id, label=label, capability='text', supports_image_inputs=True, configured=subscription_ready, enabled=subscription_ready))
    selected = next((item for item in options if item.enabled), None)
    text = AIModelSelection(provider=selected.provider, model=selected.model, access_method=selected.access_method, connection_id=selected.connection_id) if selected else AIModelSelection(provider='openai_codex', model=DEFAULT_CODEX_TEXT_MODEL, access_method='chatgpt_subscription')
    return AIModelCatalog(text=options, realtime=[], defaults={'text': text, 'realtime': AIModelSelection(provider='openai', model='gpt-realtime', access_method='personal_api')}, agent_backends={key: [AIAgentBackendOption(id='pi', label='Pi', enabled=runtime)] for key in ('teaching', 'source')})

def resolve_personal_model_selection(selection, *, user_id, catalog_builder=None):
    catalog = catalog_builder(user_id) if catalog_builder else personal_model_catalog(user_id, selection.connection_id if selection else None)
    chosen = selection or catalog.defaults['text']
    matches = [item for item in catalog.text if item.provider == chosen.provider and item.model == chosen.model and (chosen.access_method is None or item.access_method == chosen.access_method) and (chosen.connection_id is None or item.connection_id == chosen.connection_id)]
    if not matches or not all((item.enabled for item in matches)):
        raise RuntimeError('The selected personal model is unavailable. Connect or choose a model to continue.')
    methods = {item.access_method for item in matches}
    if len(methods) != 1:
        raise RuntimeError('Choose the model account or quota source explicitly.')
    return chosen.model_copy(update={'agent_backend': 'pi', 'access_method': next(iter(methods))})

def local_model_services():
    from openclass_core.services.studio_remote_models import remote_platform_adapter

    def source_proxy():
        from types import SimpleNamespace
        from openclass_core.services.studio_ports import host_service
        from openclass_core.services.studio_platform_client import local_platform_client
        client = local_platform_client(host_service('local_profile').id)
        connection = client.get('connection')
        if not client.status()['connected'] or 'models.invoke' not in (connection or {}).get('scopes', []):
            raise RuntimeError('Connect OpenClass with model permission before using platform source tools.')
        return SimpleNamespace(configured=True, base_url=client.origin + '/api/studio/v1/source-proxy', api_key=connection['access_token'], timeout_seconds=1800)

    def catalog(owner, connection_id=None):
        from openclass_core.services.studio_platform_client import local_platform_client
        value = personal_model_catalog(owner, connection_id)
        return value.model_copy(update={'text': [*value.text, *local_platform_client(owner).models()]})

    def resolve(selection, *, user_id, **_kwargs):
        return resolve_personal_model_selection(selection, user_id=user_id, catalog_builder=lambda owner: catalog(owner, selection.connection_id if selection else None))
    return {'model_catalog': catalog, 'resolve_model_selection': resolve, 'default_text_selection': lambda **kwargs: AIModelSelection(provider='openai_codex', model=kwargs.get('model') or DEFAULT_CODEX_TEXT_MODEL, access_method='chatgpt_subscription', reasoning_effort=kwargs.get('reasoning_effort'), service_tier=kwargs.get('service_tier')), 'default_realtime_selection': lambda: AIModelSelection(provider='openai', model='gpt-realtime', access_method='personal_api'), 'realtime_enabled': lambda: False, 'codex_realtime_enabled': lambda: False, 'codex_realtime_configured': lambda: False, 'codex_realtime_allowed': lambda _owner: False, 'platform_model_adapter': remote_platform_adapter, 'source_proxy_config': source_proxy, 'source_proxy_allowed': lambda owner: bool(source_proxy().configured), 'credit_billing_enabled': lambda: False}
