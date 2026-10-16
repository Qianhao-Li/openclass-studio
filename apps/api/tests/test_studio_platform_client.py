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
