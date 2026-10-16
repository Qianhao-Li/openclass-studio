"""External resource consent; never creates an OpenClass sign-in identity."""
from __future__ import annotations
import base64
import hashlib
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from openclass_core.services.external_connections import ExternalConnectionError, ExternalConnectionStore
from openclass_core.services.external_oauth import ResourceOAuthClient, resource_oauth_config
PROVIDER_CAPABILITIES = {'google': ('drive_selected', 'drive_all'), 'microsoft': ('files_personal', 'files_work', 'sharepoint'), 'github': ('repositories',), 'gitlab': ('repositories',), 'notion': ('pages',), 'zotero': ('library',)}
RESOURCE_PROVIDERS = set(PROVIDER_CAPABILITIES)
PROVIDER_NOT_ENABLED_MESSAGE = 'This account provider has not been enabled by this site.'

def connection_provider_enabled(provider: str) -> bool:
    configured = os.getenv('OPENCLASS_EXTERNAL_CONNECTION_ENABLED_PROVIDERS')
    if configured is None:
        return True
    aliases = {'chatgpt_codex': 'codex'}
    enabled = {aliases.get(name, name) for name in configured.lower().replace(',', ' ').split()}
    return aliases.get(provider, provider) in enabled

def require_connection_provider_enabled(provider: str) -> None:
    if not connection_provider_enabled(provider):
        raise ExternalConnectionError(PROVIDER_NOT_ENABLED_MESSAGE, 503)

def _public_origin() -> str:
    origin = os.getenv('OPENCLASS_PUBLIC_ORIGIN', '').rstrip('/')
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' and (not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'})) or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ExternalConnectionError('Configure the public origin before connecting an account.', 503)
    return origin

def _secret(file_name: str, direct_name: str='') -> str:
    path = os.getenv(file_name, '').strip()
    if path:
        try:
            return Path(path).expanduser().read_text().strip()
        except OSError as exc:
            raise ExternalConnectionError('External account authorization is not configured.', 503) from exc
    return os.getenv(direct_name, '').strip() if direct_name else ''

def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')

class ExternalAccountAuthorization:

    def __init__(self, store: ExternalConnectionStore):
        self.store = store

    def _client(self, provider: str, capability: str):
        if capability not in PROVIDER_CAPABILITIES.get(provider, ()):
            raise ExternalConnectionError('This account capability is not supported.', 422)
        redirect_uri = _public_origin() + '/api/account/connections/callback/' + provider
        if provider in {'google', 'microsoft'}:
            return ResourceOAuthClient(resource_oauth_config(provider, capability, redirect_uri))
        if provider in {'github', 'gitlab'}:
            from openclass_core.services.repository_oauth import GitHubOAuthClient, GitLabOAuthClient
            client = (GitHubOAuthClient if provider == 'github' else GitLabOAuthClient).from_env(redirect_uri)
            if not client.configured:
                raise ExternalConnectionError('Repository account authorization is not configured.', 503)
            return client
        if provider == 'notion':
            from openclass_core.services.notion_client import NotionClient
            client_id = os.getenv('OPENCLASS_NOTION_CLIENT_ID', '').strip()
            secret = _secret('OPENCLASS_NOTION_CLIENT_SECRET_FILE', 'OPENCLASS_NOTION_CLIENT_SECRET')
            if not client_id or not secret:
                raise ExternalConnectionError('Notion authorization is not configured.', 503)
            return NotionClient(client_id, secret, redirect_uri)
        if provider == 'zotero':
            from openclass_core.services.zotero_client import ZoteroClient
            client_key = os.getenv('OPENCLASS_ZOTERO_CLIENT_KEY', '').strip()
            secret = _secret('OPENCLASS_ZOTERO_CLIENT_SECRET_FILE', 'OPENCLASS_ZOTERO_CLIENT_SECRET')
            if not client_key or not secret:
                raise ExternalConnectionError('Zotero authorization is not configured.', 503)
            return ZoteroClient(client_key, secret, redirect_uri)
        raise ExternalConnectionError('This account provider is not supported.', 404)

    def provider_statuses(self) -> list[dict]:
        providers = (('google', 'Google', ['drive_selected', 'drive_all']), ('github', 'GitHub', ['repositories']), ('gitlab', 'GitLab', ['repositories']), ('microsoft', 'Microsoft', ['files_personal', 'files_work', 'sharepoint']), ('notion', 'Notion', ['pages']), ('zotero', 'Zotero', ['library']))
        result = []
        for provider, label, capabilities in providers:
            reason = ''
            try:
                require_connection_provider_enabled(provider)
                client = self._client(provider, capabilities[0])
                configured = bool(getattr(client, 'configured', True))
                if not configured:
                    reason = 'The provider application is not configured on this server.'
            except ExternalConnectionError as exc:
                configured = False
                reason = str(exc)
            result.append({'provider': provider, 'label': label, 'capabilities': capabilities, 'configured': configured, 'reason': reason})
        return result

    def revoke_upstream(self, connection: dict, credentials: dict) -> str:
        provider = connection['provider']
        if provider == 'google':
            from openclass_core.services.google_drive_client import GoogleDriveClient
            from openclass_core.services.google_drive_sources import DriveConfig
            import httpx
            grants = credentials.get('grants', {})
            token = next((item.get('refresh_token') for item in grants.values() if isinstance(item, dict) and item.get('refresh_token')), None)
            if not token:
                raise ExternalConnectionError('Google account access stopped locally; revoke its project grant in Google settings.', 503)
            config = resource_oauth_config('google', 'drive_selected', _public_origin() + '/api/account/connections/callback/google')
            with httpx.Client(timeout=30, follow_redirects=False) as http:
                GoogleDriveClient(DriveConfig(config.client_id, config.client_secret, b'', _public_origin()), http).revoke_project_authorization(token)
            return 'succeeded'
        if provider == 'notion':
            self._client('notion', 'pages').revoke(str(credentials.get('access_token') or ''))
            return 'succeeded'
        if provider == 'zotero':
            self._client('zotero', 'library').revoke(str(credentials.get('api_key') or ''))
            return 'succeeded'
        if provider in {'github', 'gitlab'}:
            import httpx
            client = self._client(provider, 'repositories')
            token = str(credentials.get('access_token') or '')
            if not token:
                raise ExternalConnectionError('Repository authorization could not be revoked upstream.', 503)
            with httpx.Client(timeout=30, follow_redirects=False) as http:
                if provider == 'github':
                    response = http.delete('https://api.github.com/applications/' + client.client_id + '/token', auth=(client.client_id, client.client_secret), headers={'Accept': 'application/vnd.github+json'}, json={'access_token': token})
                    success = response.status_code == 204
                else:
                    response = http.post('https://gitlab.com/oauth/revoke', data={'client_id': client.client_id, 'client_secret': client.client_secret, 'token': token})
                    success = response.status_code == 200
            if not success:
                raise ExternalConnectionError("Repository access stopped locally; revoke the app in the provider's settings.", 503)
            return 'succeeded'
        return 'provider_managed'

    def begin(self, owner: str, session_token: str, provider: str, capability: str, return_path: str='/profile?tab=settings&section=account', connection_id: str | None=None) -> dict:
        if provider not in RESOURCE_PROVIDERS:
            raise ExternalConnectionError('This account provider is not supported.', 404)
        require_connection_provider_enabled(provider)
        if capability not in PROVIDER_CAPABILITIES[provider]:
            raise ExternalConnectionError('This account capability is not supported.', 422)
        if connection_id:
            previous = self.store.get_connection(owner, connection_id)
            if previous is None or previous['provider'] != provider or previous['status'] == 'disconnected':
                raise ExternalConnectionError('Connected account not found.', 404)
        versions = self.store.authorization_versions(owner, provider)
        client = self._client(provider, capability)
        payload = {'capability': capability, 'connection_id': connection_id, 'authorization_versions': versions}
        if provider == 'zotero':
            request = client.request_token()
            temporary = str(request['oauth_token'])
            payload['request_secret'] = str(request['oauth_token_secret'])
            state = self.store.create_attempt(owner, provider, session_token, 'connect', payload, return_path, state=temporary)
            return {'provider': provider, 'authorization_url': client.authorization_url(state), 'attempt_id': hashlib.sha256(state.encode()).hexdigest()}
        verifier = secrets.token_urlsafe(64)
        payload['code_verifier'] = verifier
        state = self.store.create_attempt(owner, provider, session_token, 'connect', payload, return_path)
        if provider in {'google', 'microsoft'}:
            url = client.authorization_url(state, _challenge(verifier))
        elif provider in {'github', 'gitlab'}:
            url = client.authorization_url(state=state, code_challenge=_challenge(verifier))
        else:
            url = client.authorization_url(state)
        return {'provider': provider, 'authorization_url': url, 'attempt_id': hashlib.sha256(state.encode()).hexdigest()}

    def complete(self, owner: str, session_token: str, provider: str, state: str, code: str='', verifier: str='') -> tuple[dict, str]:
        require_connection_provider_enabled(provider)
        attempt = self.store.consume_attempt(owner, provider, session_token, state)
        details = attempt['payload']
        if attempt['purpose'] != 'connect':
            raise ExternalConnectionError('Unexpected account authorization purpose.', 409)
        capability = details['capability']
        if capability not in PROVIDER_CAPABILITIES.get(provider, ()):
            raise ExternalConnectionError('This account capability is not supported.', 422)
        client = self._client(provider, capability)
        if provider in {'google', 'microsoft'}:
            token = client.exchange_code(code, details['code_verifier'])
            profile = client.profile(token['access_token'])
            issuer = 'https://accounts.google.com' if provider == 'google' else 'https://login.microsoftonline.com'
            subject = str(profile['sub'])
            tenant = str(profile.get('tid') or '') if provider == 'microsoft' else ''
            label = str(profile.get('email') or profile.get('name') or subject)
            credentials = {'grants': {capability: token}}
            grants = {capability: {'granted': True, 'enabled': True, 'scopes': token['scope'].split()}}
        elif provider in {'github', 'gitlab'}:
            token = client.exchange_code(code, details['code_verifier'])
            profile = client.identity(token['access_token'])
            issuer, subject, label, tenant = (profile['issuer'], profile['subject'], profile['display_name'], '')
            credentials = token
            grants = {'repositories': {'granted': True, 'enabled': True, 'read_only': True}}
        elif provider == 'notion':
            token = client.exchange_code(code)
            profile = client.inspect_access(token['access_token'])
            issuer, tenant = ('https://api.notion.com', str(token['workspace_id']))
            subject = str(profile.get('user_id') or profile['bot_id']) + ':' + str(profile['bot_id'])
            label = str(profile.get('workspace_name') or token.get('workspace_name') or tenant)
            credentials = {'access_token': token['access_token'], 'refresh_token': token['refresh_token']}
            grants = {'pages': {'granted': bool(profile['grants']['read_content']), 'enabled': True, 'read_only': True, 'bot_id': profile['bot_id']}}
        elif provider == 'zotero':
            token = client.exchange_token(state, details['request_secret'], verifier)
            issuer, subject, tenant = ('https://www.zotero.org', str(token['user_id']), '')
            label = str(token.get('username') or subject)
            credentials = {'api_key': token['api_key']}
            grants = {'library': {'granted': True, 'enabled': True, 'read_only': True, 'access': token['grants']}}
        else:
            raise ExternalConnectionError('This account provider is not supported.', 404)
        selected_id = details.get('connection_id')
        scope = {'mode': 'selected', 'resource_ids': []}
        grants = {name: {**grant, 'resource_scope': dict(scope)} for name, grant in grants.items()}
        versions = details.get('authorization_versions')
        if not isinstance(versions, dict):
            raise ExternalConnectionError('Start a new account authorization.', 409)
        saved = self.store.save_connection(owner, provider, issuer, subject, label, credentials, grants, scope, tenant=tenant, connection_id=selected_id, authorization_versions=versions, merge_grants=True)
        return (saved, attempt['return_path'])
