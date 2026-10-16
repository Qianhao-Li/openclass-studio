from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openclass_core.routers.studio_bootstrap import router
from openclass_core.services.studio_identity import LocalStudioIdentity

TOKEN = "a" * 48
ORIGIN = "http://127.0.0.1:3510"


def client(root: Path, *, peer="127.0.0.1", token=TOKEN):
    identity = LocalStudioIdentity(root, launch_token=token, web_origin=ORIGIN, api_origin="http://127.0.0.1:8510")
    app = FastAPI()
    app.state.local_studio_identity = identity
    app.state.studio_identity = identity.resolve
    app.include_router(router)
    return TestClient(app, base_url="http://127.0.0.1:8510", client=(peer, 50000)), identity


def test_local_profile_reopens_without_platform_account(tmp_path):
    first, identity = client(tmp_path)
    assert first.get("/api/workbench/v1/bootstrap").status_code == 401
    opened = first.post("/api/workbench/v1/session", headers={"Origin": ORIGIN, "X-OpenClass-Launch-Token": TOKEN})
    assert opened.status_code == 200
    assert opened.json()["principal"]["kind"] == "local"
    assert "httponly" in opened.headers["set-cookie"].lower()
    assert first.get("/api/workbench/v1/bootstrap").status_code == 200
    second, reopened = client(tmp_path, token="b" * 48)
    assert reopened.principal.id == identity.principal.id
    assert not list(tmp_path.glob("*.sqlite*"))
    assert second.get("/api/workbench/v1/bootstrap").status_code == 401


@pytest.mark.parametrize("peer,origin,token,host", [
    ("192.0.2.1", ORIGIN, TOKEN, "127.0.0.1:8510"),
    ("127.0.0.1", "https://untrusted.example", TOKEN, "127.0.0.1:8510"),
    ("127.0.0.1", ORIGIN, "wrong", "127.0.0.1:8510"),
    ("127.0.0.1", ORIGIN, TOKEN, "untrusted.example:8510"),
    ("127.0.0.1", "", TOKEN, "127.0.0.1:8510"),
])
def test_bootstrap_rejects_untrusted_transport(tmp_path, peer, origin, token, host):
    browser, _ = client(tmp_path, peer=peer)
    result = browser.post("/api/workbench/v1/session", headers={"Origin": origin, "Host": host, "X-OpenClass-Launch-Token": token})
    assert result.status_code in {401, 403}


def test_profile_symlink_is_rejected(tmp_path):
    external = tmp_path / "external"
    external.write_text("{}")
    (tmp_path / "profile.json").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        client(tmp_path)


def test_profiles_keep_separate_sessions_on_the_same_loopback_host(tmp_path):
    first,a=client(tmp_path/'first');second,b=client(tmp_path/'second')
    assert a.cookie_name!=b.cookie_name
    headers={'Origin':ORIGIN,'X-OpenClass-Launch-Token':TOKEN}
    first.post('/api/workbench/v1/session',headers=headers)
    second.cookies.update(first.cookies)
    second.post('/api/workbench/v1/session',headers=headers)
    first.cookies.update(second.cookies)
    assert first.get('/api/workbench/v1/bootstrap').json()['principal']['id']==a.principal.id
    assert second.get('/api/workbench/v1/bootstrap').json()['principal']['id']==b.principal.id
