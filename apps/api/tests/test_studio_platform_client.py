import json
import httpx
import pytest
from fastapi import HTTPException
from openclass_core.services.studio_state import StudioStateStore
from openclass_core.services.studio_platform_client import PlatformClient


def test_device_secret_and_platform_token_stay_in_profile_encrypted_storage(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENCLASS_STUDIO_PLATFORM_ORIGIN','https://platform.example')
    def remote(request):
        body=json.loads(request.content) if request.content else {}
        if request.url.path.endswith('/device'):
            assert body['scopes']==['models.invoke']
            return httpx.Response(200,json={'device_code':'private-device-secret','user_code':'ABCD-EFGH','interval':5,'expires_in':600,
                'verification_uri':'https://platform.example/studio-connect','verification_uri_complete':'https://platform.example/studio-connect?user_code=ABCD-EFGH'})
        if request.url.path.endswith('/token'):
            assert body=={'device_code':'private-device-secret'}
            return httpx.Response(200,json={'access_token':'private-platform-token','token_type':'Bearer','account_id':'account','token_id':'token-1','expires_at':9999999999,'scopes':['models.invoke']})
        assert request.headers['authorization']=='Bearer private-platform-token'
        return httpx.Response(200,json={'status':'revoked'})
    state=StudioStateStore(tmp_path)
    client=PlatformClient(state,'local-owner',transport=httpx.MockTransport(remote))
    pending=client.start(['models.invoke'])
    assert 'device_code' not in pending
    assert client.poll(pending['attempt_id'])['status']=='connected'
    assert 'access_token' not in client.status()
    assert b'private-platform-token' not in state.path.read_bytes()
    assert b'private-device-secret' not in state.path.read_bytes()
    assert not PlatformClient(state,'other-profile').status()['connected']
    assert client.disconnect()['remote_revoked']
    assert not client.status()['connected']


def test_authorization_does_not_follow_an_unexpected_platform_origin(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENCLASS_STUDIO_PLATFORM_ORIGIN','https://platform.example')
    transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'device_code':'s','expires_in':600,'interval':5,'verification_uri':'https://other.example','verification_uri_complete':'https://other.example'}))
    with pytest.raises(HTTPException) as error:PlatformClient(StudioStateStore(tmp_path),'owner',transport=transport).start(['models.invoke'])
    assert error.value.status_code==502


def test_account_status_matches_the_approved_account_and_revocation_removes_local_access(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENCLASS_STUDIO_PLATFORM_ORIGIN', 'https://platform.example')
    revoked = [False]
    def remote(request):
        assert request.url.path == '/api/studio/v1/account'
        assert request.headers['authorization'] == 'Bearer private-platform-token'
        if revoked[0]:
            return httpx.Response(401, json={'detail': 'Device revoked'})
        return httpx.Response(200, json={'account_id': 'account', 'email': 'member@example.test', 'display_name': 'Member',
                                       'wallet': {'balance_credits': 50, 'available_credits': 45, 'reserved_credits': 5}})
    client = PlatformClient(StudioStateStore(tmp_path), 'local-owner', transport=httpx.MockTransport(remote))
    client.put('connection', {'origin': client.origin, 'access_token': 'private-platform-token', 'account_id': 'account',
                              'token_id': 'device', 'expires_at': 9999999999, 'scopes': ['models.invoke']})
    status = client.account_status()
    assert status['connected'] and status['email'] == 'member@example.test'
    assert status['wallet']['available_credits'] == 45
    assert 'access_token' not in status
    revoked[0] = True
    assert not client.account_status()['connected']
    assert client.get('connection') is None


@pytest.mark.parametrize('mode', ['different_account', 'offline'])
def test_another_account_or_unreachable_platform_cannot_be_reported_as_signed_in(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('OPENCLASS_STUDIO_PLATFORM_ORIGIN', 'https://platform.example')
    def remote(request):
        if mode == 'offline':
            raise httpx.ConnectError('Platform is offline', request=request)
        return httpx.Response(200, json={'account_id': 'other-account', 'email': 'other@example.test'})
    client = PlatformClient(StudioStateStore(tmp_path), 'local-owner', transport=httpx.MockTransport(remote))
    client.put('connection', {'origin': client.origin, 'access_token': 'private-platform-token', 'account_id': 'account',
                              'token_id': 'device', 'expires_at': 9999999999, 'scopes': ['models.invoke']})
    status = client.account_status()
    assert not status['connected'] and 'email' not in status
    assert client.get('connection')['account_id'] == 'account'
