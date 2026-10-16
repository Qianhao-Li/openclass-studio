from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import AIModelCatalog, AIProviderCredentialInput, AIProviderCredentialStatus
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.studio_dependencies import current_user
from openclass_core.services.studio_state import StudioPreferences
from openclass_core.services.studio_personal_models import personal_model_catalog
from openclass_core.services.pi_provider_catalog import pi_api_provider_catalog, pi_api_provider, pi_provider_configuration_fields
from openclass_core.services.pi_agent_runtime import pi_personal_api_configured, save_pi_personal_api_key, remove_pi_personal_api_key
router = APIRouter()

class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    settings: StudioPreferences
    expected_revision: int = Field(ge=0)

@router.get('/api/workbench/v1/preferences')
def preferences(request: Request, principal: WorkspacePrincipal=Depends(current_user)):
    return request.app.state.studio_state.preferences(principal.id)

@router.put('/api/workbench/v1/preferences')
def save_preferences(request: Request, payload: PreferencesUpdate, principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('models.manage')
    return request.app.state.studio_state.save_preferences(principal.id, payload.settings, payload.expected_revision)

@router.get('/api/ai-models', response_model=AIModelCatalog)
@router.get('/api/workbench/v1/models', response_model=AIModelCatalog)
def models(principal: WorkspacePrincipal=Depends(current_user), connection_id: str | None=None):
    return personal_model_catalog(principal.id, connection_id)

def _status(provider: str, principal: WorkspacePrincipal):
    normalized = provider.strip().lower().replace('_', '-')
    item = pi_api_provider(normalized)
    if item is None:
        raise HTTPException(400, 'This model provider does not support personal API keys.')
    return AIProviderCredentialStatus(provider=normalized, label=item['label'], manageable=True, configured=pi_personal_api_configured(owner_user_id=principal.id, provider=normalized), configuration_fields=pi_provider_configuration_fields(normalized))

@router.get('/api/model-credentials', response_model=list[AIProviderCredentialStatus])
def credentials(principal: WorkspacePrincipal=Depends(current_user)):
    return [_status(item['provider'], principal) for item in pi_api_provider_catalog()]

@router.put('/api/model-credentials/{provider}', response_model=AIProviderCredentialStatus)
def save_credential(provider: str, payload: AIProviderCredentialInput, principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('models.manage')
    status = _status(provider, principal)
    try:
        save_pi_personal_api_key(owner_user_id=principal.id, provider=status.provider, api_key=payload.api_key.get_secret_value(), provider_config=payload.provider_config)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return _status(status.provider, principal)

@router.delete('/api/model-credentials/{provider}', response_model=AIProviderCredentialStatus)
def delete_credential(provider: str, principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('models.manage')
    status = _status(provider, principal)
    remove_pi_personal_api_key(owner_user_id=principal.id, provider=status.provider)
    return _status(status.provider, principal)
