"""Optional remote platform connection. Its secrets and sync state stay outside RIDOC."""
from __future__ import annotations
import json
import os
import secrets
import time
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from fastapi import HTTPException
import httpx

class PlatformClient:

    def __init__(self, state, principal_id, *, transport=None):
        self.state, self.owner, self.transport = (state, principal_id, transport)
        self.origin = os.getenv('OPENCLASS_STUDIO_PLATFORM_ORIGIN', 'https://open-classes.com').rstrip('/')
        url = urlsplit(self.origin)
        if url.username or url.password or url.path or url.query or url.fragment or (url.scheme != 'https' and (not (url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'} and (os.getenv('OPENCLASS_STUDIO_ALLOW_TEST_PLATFORM') == '1')))):
            raise ValueError('Studio requires an HTTPS platform origin.')
        self.cipher = Fernet(state.key_file.read_bytes().strip())
        with state.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS studio_platform_state (principal_id TEXT NOT NULL, name TEXT NOT NULL, encrypted BLOB NOT NULL, PRIMARY KEY(principal_id,name))')

    def get(self, name):
        with self.state.connection() as db:
            row = db.execute('SELECT encrypted FROM studio_platform_state WHERE principal_id=? AND name=?', (self.owner, name)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def put(self, name, value):
        with self.state.transaction() as db:
            if value is None:
                db.execute('DELETE FROM studio_platform_state WHERE principal_id=? AND name=?', (self.owner, name))
            else:
                db.execute('INSERT INTO studio_platform_state VALUES(?,?,?) ON CONFLICT(principal_id,name) DO UPDATE SET encrypted=excluded.encrypted', (self.owner, name, self.cipher.encrypt(json.dumps(value, separators=(',', ':')).encode())))

    def claim(self, name, value):
        with self.state.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT encrypted FROM studio_platform_state WHERE principal_id=? AND name=?', (self.owner, name)).fetchone()
            if row:
                existing = json.loads(self.cipher.decrypt(row[0]))
                if existing.get('fingerprint') != value.get('fingerprint'):
                    raise HTTPException(409, 'Another sync operation owns this revision. Local contents are unchanged.')
                return existing
            db.execute('INSERT INTO studio_platform_state VALUES(?,?,?)', (self.owner, name, self.cipher.encrypt(json.dumps(value).encode())))
        return value

    def request(self, method, path, *, authenticated=True, connection=None, **kwargs):
        headers = {}
        if authenticated:
            connection = connection or self.get('connection')
            if not connection or connection['origin'] != self.origin or connection['expires_at'] <= time.time():
                raise HTTPException(401, 'Connect this Studio to OpenClass again.')
            headers['Authorization'] = 'Bearer ' + connection['access_token']
        try:
            duration = 1800 if path.startswith('model-calls') and (not path.endswith('/cancel')) else 180 if path.startswith('courses/') else 10
            with httpx.Client(base_url=self.origin + '/api/studio/v1/', timeout=httpx.Timeout(duration, connect=5, pool=5), follow_redirects=False, trust_env=False, transport=self.transport) as client:
                response = client.request(method, path.lstrip('/'), headers=headers, **kwargs)
            if response.status_code >= 300:
                raise HTTPException(response.status_code, response.json().get('detail', 'The platform request failed.'))
            return response
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, 'OpenClass is unreachable. Your local course is unchanged.') from error

    def status(self):
        row = self.get('connection')
        return {'origin': self.origin, 'connected': bool(row and row['origin'] == self.origin and (row['expires_at'] > time.time())), **({key: row[key] for key in ('account_id', 'token_id', 'expires_at', 'scopes')} if row else {})}

    def account_status(self):
        status = self.status()
        if not status['connected']:
            return status
        try:
            account = self.request('GET', 'account').json()
            if account.get('account_id') != status['account_id']:
                raise HTTPException(502, 'The platform returned a different account. Reconnect this Studio.')
            return {**status, **{key: account[key] for key in ('email', 'display_name', 'wallet') if key in account}}
        except HTTPException as error:
            if error.status_code in {401, 403}:
                self.put('connection', None)
                self.put('model_catalog', None)
                return {'origin': self.origin, 'connected': False, 'message': 'Sign in to OpenClass again. This device authorization is unavailable.'}
            return {**status, 'connected': False, 'message': 'OpenClass account status is unavailable. Local courses remain on this device.'}

    def start(self, scopes):
        grant = self.request('POST', 'device', authenticated=False, json={'label': 'OpenClass Studio', 'scopes': scopes}).json()
        for key in ('verification_uri', 'verification_uri_complete'):
            url = urlsplit(grant[key])
            if url.scheme + '://' + url.netloc != self.origin:
                raise HTTPException(502, 'The platform returned an invalid authorization URL.')
        attempt = secrets.token_urlsafe(24)
        self.put('pending', {'id': attempt, 'device_code': grant['device_code'], 'origin': self.origin, 'expires_at': time.time() + grant['expires_in'], 'interval': max(5, grant['interval']), 'next_poll': 0})
        return {key: value for key, value in grant.items() if key != 'device_code'} | {'attempt_id': attempt}

    def poll(self, attempt):
        pending = self.get('pending')
        if not pending or pending['id'] != attempt or pending['origin'] != self.origin:
            raise HTTPException(404, 'This platform connection request is unavailable.')
        if pending['expires_at'] <= time.time():
            self.put('pending', None)
            return {'status': 'expired'}
        if pending['next_poll'] > time.time():
            return {'status': 'pending', 'interval': pending['interval']}
        pending['next_poll'] = time.time() + pending['interval']
        self.put('pending', pending)
        result = self.request('POST', 'device/token', authenticated=False, json={'device_code': pending['device_code']}).json()
        if result.get('error') in {'authorization_pending', 'slow_down'}:
            return {'status': 'pending', 'interval': pending['interval']}
        if result.get('error'):
            self.put('pending', None)
            return {'status': 'denied' if result['error'] == 'access_denied' else 'expired'}
        if result.get('token_type') != 'Bearer' or not result.get('access_token'):
            raise HTTPException(502, 'The platform did not return a valid connection.')
        self.put('connection', {**result, 'origin': self.origin})
        self.put('pending', None)
        return {'status': 'connected', **self.status()}

    def disconnect(self):
        revoked = False
        failure = None
        try:
            if self.status()['connected']:
                self.request('POST', 'disconnect')
                revoked = True
        except HTTPException:
            failure = 'Disconnected on this device. The platform could not confirm revocation; revoke this device from your OpenClass connection settings.'
        finally:
            self.put('connection', None)
            self.put('pending', None)
            self.put('model_catalog', None)
        return {'connected': False, 'remote_revoked': revoked, 'message': failure}

    def models(self):
        from openclass_core.models import AIModelCatalog
        if not self.status()['connected']:
            return []
        try:
            catalog = AIModelCatalog.model_validate(self.request('GET', 'models').json())
            models = [item for item in catalog.text if item.access_method == 'platform_credits']
            self.put('model_catalog', [item.model_dump(mode='json') for item in models])
            return models
        except HTTPException:
            from openclass_core.models import AIModelOption
            return [AIModelOption.model_validate(item).model_copy(update={'enabled': False, 'configured': False}) for item in self.get('model_catalog') or []]

def local_platform_client(owner):
    from openclass_core.services.studio_ports import host_service
    return PlatformClient(host_service('local_state'), owner)
